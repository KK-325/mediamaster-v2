import re
import sqlite3
import subprocess
import threading
import requests
import bcrypt
import psutil
from flask import Flask, g, render_template, request, redirect, url_for, jsonify, session, flash, session, Response, abort
from functools import wraps
from werkzeug.exceptions import InternalServerError
from datetime import timedelta
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename
from flask import stream_with_context
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Response, stream_with_context
import json
import threading
import os
import time
import logging
import json
import glob
import threading
import uuid
from collections import deque

# Ensure runtime directories exist (Windows maps '/tmp' to 'C:\\tmp')
os.makedirs("/tmp/log", exist_ok=True)
os.makedirs("/tmp/index", exist_ok=True)

# 配置日志
# 创建独立的 logger 实例
logger = logging.getLogger("MediaMasterLogger")
logger.setLevel(logging.INFO)

# 禁用日志传播
logger.propagate = False

# 配置日志处理器
if not logger.handlers:
    file_handler = logging.FileHandler("/tmp/log/app.log", mode='w')
    stream_handler = logging.StreamHandler()

    # 设置日志格式
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    file_handler.setFormatter(formatter)
    stream_handler.setFormatter(formatter)

    # 添加处理器到 logger
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)
# 定义版本号
def get_app_version():
    """
    从 versions 文件中读取版本号
    """
    try:
        with open("versions", "r") as file:
            return file.read().strip()
    except FileNotFoundError:
        logger.warning("versions 文件未找到，使用默认版本号")
        return "unknown"

APP_VERSION = get_app_version()
app.secret_key = 'mediamaster'  # 设置一个密钥，用于会话管理
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)  # 设置会话有效期为24小时
app.config['SESSION_COOKIE_NAME'] = 'mediamaster'  # 设置会话 cookie 名称为 mediamaster
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'  # 设置会话 cookie 的 SameSite 属性

# Database path: prefer env override; fall back to docker default; for local dev fall back to workspace data.db
_db_from_env = os.environ.get('DATABASE') or os.environ.get('DB_PATH')
_default_db = _db_from_env or '/config/data.db'
if not os.path.exists(_default_db):
    _local_db = os.path.join(os.path.dirname(__file__), 'data.db')
    DATABASE = _local_db
    logger.warning(f"未找到数据库文件: {_default_db}，将使用本地数据库: {DATABASE}")
else:
    DATABASE = _default_db

# 存储进程ID的字典
running_services = {}

# 存储日志传输状态的字典
log_streaming_status = {}

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return view(**kwargs)
    return wrapped_view

def create_soft_link(src, dst):
    try:
        # 在 Windows 非管理员/未启用开发者模式时，创建软链接可能失败；这里做 best-effort
        if os.name == 'nt':
            logger.info("Windows 环境跳过头像目录软链接创建")
            return

        # 确保源目录存在
        os.makedirs(src, exist_ok=True)
        # 确保目标目录存在
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        # 创建软链接
        if not os.path.exists(dst):
            os.symlink(src, dst)
            logger.info(f"软链接创建成功: {src} -> {dst}")
        else:
            logger.info(f"软链接已存在: {dst}")
    except Exception as e:
        logger.warning(f"软链接创建失败（已忽略）: {e}")

@app.route('/login', methods=('GET', 'POST'))
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        # 获取记住我选项
        remember_me = request.form.get('remember') == 'on'  # 检查是否勾选了自动登录
        
        logger.info(f"用户 {username} 尝试登录，记住我: {remember_me}")
        
        db = get_db()
        error = None
        user = db.execute('SELECT * FROM USERS WHERE USERNAME = ?', (username,)).fetchone()
        
        if user is None:
            error = '用户名或密码错误'
            logger.warning(f"用户 {username} 登录失败: 用户不存在")
        else:
            # 检查并处理密码字段类型兼容性问题
            stored_password = user['PASSWORD']
            if isinstance(stored_password, str):
                stored_password = stored_password.encode('utf-8')
            elif not isinstance(stored_password, bytes):
                error = '用户数据格式异常，请重置密码！'
                logger.error(f"用户 {username} 登录失败: 用户数据格式异常，请重置密码！")
            else:
                # stored_password 已经是 bytes 类型
                pass
            
            # 如果没有前面的错误，继续验证密码
            if error is None:
                if not bcrypt.checkpw(password.encode('utf-8'), stored_password):
                    error = '用户名或密码错误'
                    logger.warning(f"用户 {username} 登录失败: 密码错误")
        
        if error is None:
            # 登录成功
            session.clear()
            session['user_id'] = user['ID']
            session['username'] = user['USERNAME']
            session['nickname'] = user['NICKNAME']
            session['avatar_url'] = user['AVATAR_URL']
            
            # 根据是否勾选"自动登录"设置session过期时间
            if remember_me:
                # 勾选了自动登录，设置session为30天后过期
                session.permanent = True
                app.permanent_session_lifetime = timedelta(days=30)
                logger.info(f"用户 {username} 登录成功，已启用自动登录(30天)")
            else:
                # 未勾选自动登录，设置为浏览器会话级别（关闭浏览器即失效）
                session.permanent = False
                logger.info(f"用户 {username} 登录成功，未启用自动登录(浏览器会话级别)")

            # 返回JSON响应给前端
            return jsonify({
                'success': True,
                'redirect_url': '/',
                'message': '登录成功'
            })

        # 登录失败返回错误信息
        return jsonify({
            'success': False,
            'message': error
        })

    # GET请求返回登录页面
    return render_template('login.html', version=APP_VERSION)

@app.route('/logout')
def logout():
    username = session.get('username')
    logger.info(f"用户 {username} 登出")
    session.clear()
    return redirect(url_for('login'))

# 配置允许上传的文件类型
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

# 检查文件扩展名是否合法
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# 更新用户资料路由
@app.route('/api/update_profile', methods=['POST'])
@login_required
def update_profile():
    try:
        user_id = session['user_id']
        nickname = session.get('nickname')
        logger.info(f"用户 {nickname} 更新个人资料")
        db = get_db()

        # 获取表单数据
        username = request.form.get('username')
        nickname_input = request.form.get('nickname')  # 获取昵称输入
        avatar_file = request.files.get('avatar')
        
        updated_avatar = False
        
        # 更新用户名和昵称（如果有提供）
        if username or nickname_input:
            if username and nickname_input:
                # 同时更新用户名和昵称
                db.execute('UPDATE USERS SET USERNAME = ?, NICKNAME = ? WHERE ID = ?', 
                          (username, nickname_input, user_id))
                logger.info(f"用户 {nickname} 更新了用户名和昵称: {username}, {nickname_input}")
            elif username:
                # 只更新用户名
                db.execute('UPDATE USERS SET USERNAME = ? WHERE ID = ?', (username, user_id))
                logger.info(f"用户 {nickname} 更新了用户名: {username}")
            elif nickname_input:
                # 只更新昵称
                db.execute('UPDATE USERS SET NICKNAME = ? WHERE ID = ?', (nickname_input, user_id))
                logger.info(f"用户 {nickname} 更新了昵称: {nickname_input}")
        
        # 更新头像
        if avatar_file and allowed_file(avatar_file.filename):
            upload_folder = 'static/uploads/avatars'
            os.makedirs(upload_folder, exist_ok=True)
            filename = secure_filename(avatar_file.filename)
            file_path = os.path.join(upload_folder, filename)
            avatar_file.save(file_path)
            avatar_url = f"/{upload_folder}/{filename}"
            # 注意：这里使用大写字段名 'AVATAR_URL'
            db.execute('UPDATE USERS SET AVATAR_URL = ? WHERE ID = ?', (avatar_url, user_id))
            logger.info(f"用户 {nickname} 更新了头像: {avatar_url}")
            updated_avatar = True
        elif not avatar_file:
            logger.info(f"用户 {nickname} 未选择新头像，跳过头像更新")

        # 提交数据库更改
        db.commit()
        logger.info(f"用户 {nickname} 个人资料更新成功")

        # 更新会话中的信息
        if username:
            session['username'] = username
        if nickname_input:
            session['nickname'] = nickname_input
            nickname = nickname_input  # 更新本地变量
        if updated_avatar:
            session['avatar_url'] = avatar_url

        return jsonify({"success": True, "message": "个人资料更新成功"})
    except Exception as e:
        logger.error(f"更新个人资料失败: {e}")
        return jsonify({"success": False, "message": "更新失败，请稍后再试"}), 500

# 修改密码路由
@app.route('/api/change_password', methods=['POST'])
@login_required
def change_password():
    try:
        user_id = session['user_id']
        nickname = session.get('nickname')
        logger.info(f"用户 {nickname} 请求修改密码")

        # 获取表单数据
        old_password = request.form.get('old_password')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')
        
        # 验证输入
        if not old_password or not new_password:
            logger.warning(f"用户 {nickname} 密码修改失败: 缺少必要参数")
            return jsonify(success=False, message='请提供当前密码和新密码。'), 400
            
        # 验证确认密码
        if new_password != confirm_password:
            logger.warning(f"用户 {nickname} 密码修改失败: 新密码和确认密码不一致")
            return jsonify(success=False, message='新密码和确认密码不一致。'), 400

        db = get_db()
        
        # 获取当前用户信息
        user = db.execute('SELECT * FROM USERS WHERE ID = ?', (user_id,)).fetchone()
        if not user:
            logger.error(f"用户 {nickname} 密码修改失败: 用户不存在")
            return jsonify(success=False, message='用户不存在。'), 400

        # 验证当前密码 (使用 bcrypt 验证)
        hashed_password = user['PASSWORD']
        if not isinstance(hashed_password, str):
            hashed_password = hashed_password.decode('utf-8')
            
        if not bcrypt.checkpw(old_password.encode('utf-8'), hashed_password.encode('utf-8')):
            logger.warning(f"用户 {nickname} 密码修改失败: 当前密码错误")
            return jsonify(success=False, message='当前密码错误。'), 400

        # 更新密码 (使用 bcrypt 生成新密码)
        new_hashed_password = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt())
        if isinstance(new_hashed_password, bytes):
            new_hashed_password = new_hashed_password.decode('utf-8')
            
        db.execute('UPDATE USERS SET PASSWORD = ? WHERE ID = ?', (new_hashed_password, user_id))
        db.commit()
        
        logger.info(f"用户 {nickname} 密码修改成功")
        return jsonify(success=True, message='密码修改成功！'), 200
        
    except Exception as e:
        logger.error(f"修改密码失败: {e}")
        return jsonify(success=False, message='密码修改失败，请稍后再试。'), 500

@app.errorhandler(InternalServerError)
def handle_500(error):
    logger.error(f"服务器错误: {error}")
    return render_template('500.html'), 500

@app.route('/')
@login_required
def dashboard():
    db = get_db()
    
    # 获取电影数量
    total_movies = db.execute('SELECT COUNT(*) FROM LIB_MOVIES').fetchone()[0]
    
    # 获取电视剧数量
    total_tvs = db.execute('SELECT COUNT(DISTINCT id) FROM LIB_TVS').fetchone()[0]
    
    # 获取剧集数量
    total_episodes = db.execute('SELECT SUM(LENGTH(episodes) - LENGTH(REPLACE(episodes, \',\', \'\')) + 1) FROM LIB_TV_SEASONS').fetchone()[0] or 0
     
    # 从会话中获取用户昵称和头像
    username = session.get('username')
    nickname = session.get('nickname')
    avatar_url = session.get('avatar_url')

    return render_template('dashboard.html', 
                           total_movies=total_movies, 
                           total_tvs=total_tvs, 
                           total_episodes=total_episodes, 
                           nickname=nickname, 
                           username=username, 
                           avatar_url=avatar_url, 
                           version=APP_VERSION)

@app.route('/api/system_resources', methods=['GET'])
@login_required
def system_resources():
    # 获取存储空间信息
    # 在 Docker 环境默认是 /Media；本地 Windows 环境可能不存在该路径，需要兜底
    media_path = '/Media'
    try:
        db = get_db()
        row = db.execute("SELECT VALUE FROM CONFIG WHERE OPTION = 'media_dir'").fetchone()
        if row and row[0]:
            media_path = str(row[0])
    except Exception:
        pass

    if not os.path.exists(media_path):
        if os.name == 'nt':
            drive = os.path.splitdrive(os.getcwd())[0]
            media_path = f"{drive}\\" if drive else os.getcwd()
        else:
            media_path = '/'

    try:
        disk_usage = psutil.disk_usage(media_path)
    except Exception as e:
        logger.warning(f"获取磁盘使用率失败: path={media_path}, err={e}")
        disk_usage = psutil.disk_usage(os.getcwd())
    disk_total_gb = disk_usage.total / (1024 ** 3)  # 总容量，单位为GB
    disk_used_gb = disk_usage.used / (1024 ** 3)    # 已用容量，单位为GB
    disk_usage_percent = disk_usage.percent         # 使用百分比

    # 获取 CPU 利用率
    cpu_usage_percent = psutil.cpu_percent(interval=1)

    # 获取 CPU 数量和核心数
    cpu_count_logical = psutil.cpu_count(logical=True)  # 逻辑 CPU 数量
    cpu_count_physical = psutil.cpu_count(logical=False)  # 物理 CPU 核心数

    # 获取内存信息
    memory = psutil.virtual_memory()
    memory_total_gb = memory.total / (1024 ** 3)  # 内存总量，单位为GB
    memory_used_gb = memory.used / (1024 ** 3)    # 已用内存，单位为GB
    memory_usage_percent = memory.percent         # 内存使用百分比

    # 获取下载器客户端
    try:
        client = get_downloader_client()
        # 仅支持迅雷下载器，迅雷无法直接获取实时下载速度
        net_io_sent_per_sec = 0
        net_io_recv_per_sec = 0
    except Exception as e:
        logger.error(f"获取下载器信息失败: {e}")
        net_io_sent_per_sec = 0
        net_io_recv_per_sec = 0

    # 返回系统资源数据
    return jsonify({
        "disk_total_gb": round(disk_total_gb, 2),         # 存储空间总量（GB）
        "disk_used_gb": round(disk_used_gb, 2),           # 存储空间已用容量（GB）
        "disk_usage_percent": disk_usage_percent,         # 存储空间使用百分比
        "net_io_sent": round(net_io_sent_per_sec, 2),     # 网络上传速率（KB/s）
        "net_io_recv": round(net_io_recv_per_sec, 2),     # 网络下载速率（KB/s）
        "cpu_usage_percent": cpu_usage_percent,           # CPU 利用率
        "cpu_count_logical": cpu_count_logical,           # 逻辑 CPU 数量
        "cpu_count_physical": cpu_count_physical,         # 物理 CPU 核心数
        "memory_total_gb": round(memory_total_gb, 2),     # 内存总量（GB）
        "memory_used_gb": round(memory_used_gb, 2),       # 已用内存（GB）
        "memory_usage_percent": memory_usage_percent      # 内存使用百分比
    })

@app.route('/api/system_processes', methods=['GET'])
@login_required
def system_processes():
    processes = []
    for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'cpu_percent', 'memory_percent', 'create_time']):
        try:
            # 计算运行时长（秒）
            uptime = time.time() - proc.info['create_time']
            
            # 格式化运行时长为天、小时、分钟、秒
            days = int(uptime // (3600 * 24))
            hours = int((uptime % (3600 * 24)) // 3600)
            minutes = int((uptime % 3600) // 60)
            seconds = int(uptime % 60)

            if days > 0:
                uptime_formatted = f"{days}天{hours:02d}小时"
            else:
                uptime_formatted = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            
            # 获取命令行参数
            cmdline = proc.info['cmdline']
            
            # 初始化文件名为 None
            file_name = None
            
            # 如果进程名为 'python' 或 'python3'，且 cmdline 不为 None，则尝试获取文件名
            if proc.info['name'] in ['python', 'python3'] and cmdline and len(cmdline) > 1:
                file_name = os.path.basename(cmdline[1])
            
            # 添加进程信息到列表
            processes.append({
                "pid": proc.info['pid'],
                "name": proc.info['name'],
                "file_name": file_name,
                "cpu_percent": proc.info['cpu_percent'],
                "memory_percent": proc.info['memory_percent'],
                "uptime": uptime_formatted
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            # 忽略不存在的进程、访问被拒绝的进程和僵尸进程
            continue

    return jsonify({
        "processes": processes
    })

@app.route('/api/site_status', methods=['GET'])
@login_required
def site_status():
    """
    获取站点状态信息（从文件中读取）
    """
    try:
        # 导入站点测试模块
        import sys
        import os
        import json
        sys.path.append('/app')
        
        # 动态导入站点测试模块
        if 'site_test' in sys.modules:
            import importlib
            importlib.reload(sys.modules['site_test'])
            site_test_module = sys.modules['site_test']
        else:
            import site_test
            site_test_module = site_test
            
        # 创建站点测试实例并获取配置
        tester = site_test_module.SiteTester()
        sites = tester.load_sites_config()
        
        # 读取站点启用状态
        db = get_db()
        enabled_sites = {}
        for site_name in sites.keys():
            option_name = f"{site_name.lower()}_enabled"
            try:
                result = db.execute('SELECT VALUE FROM CONFIG WHERE OPTION = ?', (option_name,)).fetchone()
                enabled_sites[site_name] = result['VALUE'] == 'True' if result else False
            except Exception as e:
                logger.error(f"读取站点 {site_name} 启用状态失败: {e}")
                enabled_sites[site_name] = False
        
        # 读取站点状态文件
        status_file_path = '/tmp/site_status.json'
        site_status_data = {}
        last_checked = None
        
        if os.path.exists(status_file_path):
            try:
                with open(status_file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    site_status_data = data.get('status', {})
                    last_checked = data.get('last_checked')
            except json.JSONDecodeError as e:
                logger.error(f"解析站点状态文件失败: {e}")
            except Exception as e:
                logger.error(f"读取站点状态文件失败: {e}")
        else:
            logger.warning("站点状态文件不存在")
        
        # 返回站点信息
        site_info = []
        for site_name, site_config in sites.items():
            site_info.append({
                'name': site_name,
                'url': site_config['base_url'],
                'keyword': site_config['keyword'],
                'enabled': enabled_sites.get(site_name, False)
            })
        
        return jsonify({
            'sites': site_info,
            'last_checked': last_checked,
            'status': site_status_data
        })
    except Exception as e:
        logger.error(f"获取站点状态失败: {e}")
        return jsonify({'error': '获取站点状态失败'}), 500

@app.route('/api/check_site_status', methods=['POST'])
@login_required
def check_site_status():
    """
    手动检查站点状态并更新状态文件
    """
    try:
        import sys
        import os
        import json
        sys.path.append('/app')
        
        # 动态导入站点测试模块
        if 'site_test' in sys.modules:
            import importlib
            importlib.reload(sys.modules['site_test'])
            site_test_module = sys.modules['site_test']
        else:
            import site_test
            site_test_module = site_test
            
        # 运行站点测试
        tester = site_test_module.SiteTester()
        results = tester.run_tests()
        
        # 保存结果到文件
        status_data = {
            'status': results,
            'last_checked': time.strftime('%Y-%m-%d %H:%M:%S')
        }
        
        with open('/tmp/site_status.json', 'w', encoding='utf-8') as f:
            json.dump(status_data, f, ensure_ascii=False, indent=2)
        
        return jsonify({
            'status': results,
            'last_checked': status_data['last_checked']
        })
    except Exception as e:
        logger.error(f"检查站点状态失败: {e}")
        return jsonify({'error': '检查站点状态失败'}), 500

@app.route('/search', methods=['GET'])
@login_required
def search():
    query = request.args.get('q', '').strip()
    nickname = session.get('nickname')
    avatar_url = session.get('avatar_url')
    return render_template('search.html', query=query, nickname=nickname, avatar_url=avatar_url, version=APP_VERSION)

@app.route('/api/search', methods=['GET'])
@login_required
def api_search():
    db = get_db()
    query = request.args.get('q', '').strip()
    results = {
        'movies': [],
        'tvs': []
    }

    if query:
        # 查询电影并按年份排序
        movies = db.execute('SELECT * FROM LIB_MOVIES WHERE title LIKE ? ORDER BY year ASC', ('%' + query + '%',)).fetchall()
        
        # 查询电视剧并获取其季信息
        tvs = db.execute('SELECT * FROM LIB_TVS WHERE title LIKE ? ORDER BY title ASC', ('%' + query + '%',)).fetchall()

        # 处理电影结果
        for movie in movies:
            results['movies'].append({
                'type': 'movie',
                'id': movie['id'],
                'title': movie['title'],
                'year': movie['year'],
                'tmdb_id': movie['tmdb_id']
            })

        # 处理电视剧结果
        for tv in tvs:
            # 获取该电视剧的所有季信息，并按季数排序
            seasons = db.execute('SELECT season, episodes FROM LIB_TV_SEASONS WHERE tv_id = ? ORDER BY season ASC', (tv['id'],)).fetchall()
            results['tvs'].append({
                'type': 'tv',
                'id': tv['id'],
                'title': tv['title'],
                'year': tv['year'],
                'tmdb_id': tv['tmdb_id'],
                'seasons': [{'season': s['season'], 'episodes': s['episodes']} for s in seasons]
            })
    
    # 获取TMDB配置信息
    tmdb_config = {
        'tmdb_api_key': db.execute('SELECT VALUE FROM CONFIG WHERE OPTION = ?', ('tmdb_api_key',)).fetchone()['VALUE']
    }
    
    return jsonify({
        'query': query,
        'results': results,
        'tmdb_config': tmdb_config
    })

@app.route('/library')
@login_required
def library():
    try:
        db = get_db()
        page = int(request.args.get('page', 1))
        per_page = 24
        offset = (page - 1) * per_page
        media_type = request.args.get('type', 'movies')

        # 获取电影或电视剧的总数
        total_movies = db.execute('SELECT COUNT(*) FROM LIB_MOVIES').fetchone()[0]
        total_tvs = db.execute('SELECT COUNT(DISTINCT id) FROM LIB_TVS').fetchone()[0]

        if media_type == 'movies':
            movies = db.execute('SELECT id, title, year, tmdb_id FROM LIB_MOVIES ORDER BY year DESC LIMIT ? OFFSET ?', (per_page, offset)).fetchall()
            tv_data = []
        elif media_type == 'tvs':
            movies = []
            # 查询电视剧基本信息
            tv_ids = db.execute('SELECT id FROM LIB_TVS ORDER BY year DESC LIMIT ? OFFSET ?', (per_page, offset)).fetchall()
            tv_ids = [tv['id'] for tv in tv_ids]

            # 获取这些电视剧的所有季信息
            tv_seasons = db.execute('''
                SELECT t1.id, t1.title, t2.season, t2.episodes, t1.year, t1.tmdb_id
                FROM LIB_TVS AS t1 
                JOIN LIB_TV_SEASONS AS t2 ON t1.id = t2.tv_id 
                WHERE t1.id IN ({})
                ORDER BY t1.year DESC, t1.id, t2.season 
            '''.format(','.join(['?'] * len(tv_ids))), tv_ids).fetchall()

            # 将相同电视剧的季信息合并，并计算总集数
            tv_data = {}
            for tv in tv_seasons:
                if tv['id'] not in tv_data:
                    tv_data[tv['id']] = {
                        'id': tv['id'],
                        'title': tv['title'],
                        'year': tv['year'],
                        'tmdb_id': tv['tmdb_id'],
                        'seasons': [],
                        'total_episodes': 0
                    }
                
                # 兼容处理 episodes 字段（可能是整数或字符串）
                episodes = tv['episodes']
                if isinstance(episodes, int):
                    episodes = str(episodes)

                # 解析 episodes 字符串，计算总集数
                episodes_list = episodes.split(',')
                num_episodes = len(episodes_list)

                tv_data[tv['id']]['seasons'].append({
                    'season': tv['season'],
                    'episodes': num_episodes  # 季的集数
                })
                tv_data[tv['id']]['total_episodes'] += num_episodes  # 累加总集数
            tv_data = list(tv_data.values())
        else:
            movies = []
            tv_data = []

        # 从数据库中读取 tmdb_api_key
        tmdb_api_key = db.execute('SELECT VALUE FROM CONFIG WHERE OPTION = ?', ('tmdb_api_key',)).fetchone()
        tmdb_api_key = tmdb_api_key['VALUE'] if tmdb_api_key else None

        # 从会话中获取用户昵称和头像
        nickname = session.get('nickname')
        avatar_url = session.get('avatar_url')

        return render_template('library.html', 
                               movies=movies, 
                               tv_data=tv_data, 
                               page=page, 
                               per_page=per_page, 
                               total_movies=total_movies, 
                               total_tvs=total_tvs, 
                               media_type=media_type, 
                               tmdb_api_key=tmdb_api_key,
                               nickname=nickname,
                               avatar_url=avatar_url,
                               version=APP_VERSION)
    except Exception as e:
        logger.error(f"发生错误: {e}")
        raise InternalServerError("发生意外错误，请稍后再试。")

@app.route('/subscriptions')
@login_required
def subscriptions():
    db = get_db()
    miss_movies = db.execute('SELECT * FROM MISS_MOVIES').fetchall()
    miss_tvs = db.execute('SELECT * FROM MISS_TVS').fetchall()
    # 从数据库中读取 tmdb_api_key
    tmdb_api_key = db.execute('SELECT VALUE FROM CONFIG WHERE OPTION = ?', ('tmdb_api_key',)).fetchone()
    tmdb_api_key = tmdb_api_key['VALUE'] if tmdb_api_key else None
    # 从会话中获取用户昵称和头像
    nickname = session.get('nickname')
    avatar_url = session.get('avatar_url')
    return render_template('subscriptions.html', 
                         miss_movies=miss_movies, 
                         miss_tvs=miss_tvs, 
                         tmdb_api_key=tmdb_api_key,
                         nickname=nickname, 
                         avatar_url=avatar_url, 
                         version=APP_VERSION)

# 手动添加订阅
@app.route('/add_subscription', methods=['POST'])
@login_required
def add_subscription():
    try:
        # 获取请求数据
        data = request.json
        subscription_type = data.get('type')
        title = data.get('title')
        year = data.get('year')
        season = data.get('season', 1)  # 默认第一季
        start_episode = data.get('start_episode')
        end_episode = data.get('end_episode')
        tmdb_id = data.get('tmdb_id')  # 新增：手动添加时可选携带 tmdb_id

        # 检查必要字段
        if not subscription_type or not title or not year:
            return jsonify({"success": False, "message": "缺少必要的订阅信息"}), 400

        db = get_db()

        if subscription_type == 'tv':  # 电视剧订阅
            # 验证剧集信息
            if start_episode is None or end_episode is None:
                return jsonify({"success": False, "message": "电视剧订阅需要提供起始集和结束集"}), 400
            
            try:
                start_episode = int(start_episode)
                end_episode = int(end_episode)
                season = int(season)
            except (ValueError, TypeError):
                return jsonify({"success": False, "message": "季、起始集和结束集必须是数字"}), 400
                
            if start_episode <= 0 or end_episode <= 0 or start_episode > end_episode:
                return jsonify({"success": False, "message": "起始集和结束集必须是正整数，且起始集不能大于结束集"}), 400

            # 生成缺失的集数字符串，例如 "1,2,3,...,episodes"
            missing_episodes = ','.join(map(str, range(start_episode, end_episode + 1)))

            # 生成手动订阅的douban_id
            # 获取当前最大的manual编号
            max_id_row = db.execute(
                "SELECT MAX(CAST(SUBSTR(douban_id, 8) AS INTEGER)) as max_id FROM MISS_TVS WHERE douban_id LIKE 'manual-%'"
            ).fetchone()
            
            max_id = max_id_row['max_id'] if max_id_row['max_id'] else 0
            new_douban_id = f"manual-{max_id + 1}"

            # 检查是否已存在相同的订阅（优先用 tmdb_id 去重）
            if tmdb_id:
                existing_tv = db.execute(
                    'SELECT * FROM MISS_TVS WHERE tmdb_id = ?', (tmdb_id,)
                ).fetchone()
            else:
                existing_tv = db.execute(
                    'SELECT * FROM MISS_TVS WHERE title = ? AND year = ? AND season = ?',
                    (title, year, season)
                ).fetchone()

            if existing_tv:
                return jsonify({"success": False, "message": "该电视剧订阅已存在"}), 400

            # 插入电视剧订阅
            db.execute(
                'INSERT INTO MISS_TVS (douban_id, title, year, season, missing_episodes, tmdb_id) VALUES (?, ?, ?, ?, ?, ?)',
                (new_douban_id, title, year, season, missing_episodes, tmdb_id)
            )
            db.commit()
            logger.info(f"用户添加电视剧订阅: {title} ({year}) 季{season} 集{start_episode}-{end_episode} DOUBAN_ID: {new_douban_id}")
            return jsonify({"success": True, "message": "电视剧订阅添加成功"})

        elif subscription_type == 'movie':  # 电影订阅
            # 生成手动订阅的douban_id
            # 获取当前最大的manual编号
            max_id_row = db.execute(
                "SELECT MAX(CAST(SUBSTR(douban_id, 8) AS INTEGER)) as max_id FROM MISS_MOVIES WHERE douban_id LIKE 'manual%'"
            ).fetchone()
            
            max_id = max_id_row['max_id'] if max_id_row['max_id'] else 0
            new_douban_id = f"manual{max_id + 1}"

            # 检查是否已存在相同的订阅（优先用 tmdb_id 去重）
            if tmdb_id:
                existing_movie = db.execute(
                    'SELECT * FROM MISS_MOVIES WHERE tmdb_id = ?', (tmdb_id,)
                ).fetchone()
            else:
                existing_movie = db.execute(
                    'SELECT * FROM MISS_MOVIES WHERE title = ? AND year = ?',
                    (title, year)
                ).fetchone()

            if existing_movie:
                return jsonify({"success": False, "message": "该电影订阅已存在"}), 400

            # 插入电影订阅
            db.execute(
                'INSERT INTO MISS_MOVIES (douban_id, title, year, tmdb_id) VALUES (?, ?, ?, ?)',
                (new_douban_id, title, year, tmdb_id)
            )
            db.commit()
            logger.info(f"用户添加电影订阅: {title} ({year}) DOUBAN_ID: {new_douban_id}")
            return jsonify({"success": True, "message": "电影订阅添加成功"})

        else:
            return jsonify({"success": False, "message": "无效的订阅类型"}), 400

    except Exception as e:
        logger.error(f"添加订阅失败: {e}")
        return jsonify({"success": False, "message": "添加订阅失败，请稍后再试"}), 500

# 取消热门推荐中的订阅
@app.route('/cancel_subscription', methods=['POST'])
@login_required
def cancel_subscription():
    try:
        # 获取请求数据
        data = request.json
        title = data.get('title')
        year = data.get('year')
        season = data.get('season')
        media_type = data.get('mediaType')

        # 检查必要字段
        if not title or not year or not media_type:
            return jsonify({"success": False, "message": "缺少必要的参数"}), 400

        db = get_db()
        # 优先用 tmdb_id 匹配，无 tmdb_id 时回退到 title 匹配
        # 修复：原代码仅用 title 匹配，MISS 标题被改写后用户无法取消订阅
        tmdb_id = data.get('tmdb_id')

        if media_type == 'tv':  # 电视剧取消订阅
            if tmdb_id:
                existing_tv = db.execute(
                    'SELECT * FROM MISS_TVS WHERE tmdb_id = ?', (tmdb_id,)
                ).fetchone()
            else:
                existing_tv = db.execute(
                    'SELECT * FROM MISS_TVS WHERE title = ? AND year = ? AND season = ?',
                    (title, year, season)
                ).fetchone()

            if not existing_tv:
                return jsonify({"success": False, "message": "未找到该电视剧订阅"}), 404

            # 删除订阅
            if tmdb_id:
                db.execute('DELETE FROM MISS_TVS WHERE tmdb_id = ?', (tmdb_id,))
            else:
                db.execute(
                    'DELETE FROM MISS_TVS WHERE title = ? AND year = ? AND season = ?',
                    (title, year, season)
                )
            db.commit()
            logger.info(f"用户取消电视剧订阅: {title} ({year}) 季{season}")
            return jsonify({"success": True, "message": "电视剧订阅已取消"})

        elif media_type == 'movie':  # 电影取消订阅
            if tmdb_id:
                existing_movie = db.execute(
                    'SELECT * FROM MISS_MOVIES WHERE tmdb_id = ?', (tmdb_id,)
                ).fetchone()
            else:
                existing_movie = db.execute(
                    'SELECT * FROM MISS_MOVIES WHERE title = ? AND year = ?',
                    (title, year)
                ).fetchone()

            if not existing_movie:
                return jsonify({"success": False, "message": "未找到该电影订阅"}), 404

            # 删除订阅
            if tmdb_id:
                db.execute('DELETE FROM MISS_MOVIES WHERE tmdb_id = ?', (tmdb_id,))
            else:
                db.execute('DELETE FROM MISS_MOVIES WHERE title = ? AND year = ?', (title, year))
            db.commit()
            logger.info(f"用户取消电影订阅: {title} ({year})")
            return jsonify({"success": True, "message": "电影订阅已取消"})

        else:
            return jsonify({"success": False, "message": "无效的媒体类型"}), 400

    except Exception as e:
        logger.error(f"取消订阅失败: {e}")
        return jsonify({"success": False, "message": "取消订阅失败，请稍后再试"}), 500

@app.route('/edit_subscription/<type>/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_subscription(type, id):
    db = get_db()
    if type == 'movie':
        subscription = db.execute('SELECT * FROM MISS_MOVIES WHERE id = ?', (id,)).fetchone()
    elif type == 'tv':
        subscription = db.execute('SELECT * FROM MISS_TVS WHERE id = ?', (id,)).fetchone()
    else:
        return jsonify(success=False, message="Invalid subscription type"), 400

    if request.method == 'POST':
        title = request.form['title']
        year = request.form.get('year')
        season = request.form.get('season')
        missing_episodes = request.form.get('missing_episodes')

        try:
            if type == 'movie':
                db.execute('UPDATE MISS_MOVIES SET title = ?, year = ? WHERE id = ?', (title, year, id))
            elif type == 'tv':
                db.execute('UPDATE MISS_TVS SET title = ?, season = ?, missing_episodes = ? WHERE id = ?', 
                          (title, season, missing_episodes, id))
            db.commit()
            logger.info(f"用户更新订阅: {type} ID={id}")
            return jsonify(success=True, message="订阅更新成功")
        except Exception as e:
            db.rollback()
            logger.error(f"更新订阅失败: {e}")
            return jsonify(success=False, message="更新失败，请稍后再试"), 500

    # GET 请求时返回 JSON 数据
    if subscription:
        return jsonify(dict(subscription))
    else:
        return jsonify(success=False, message="未找到订阅"), 404

@app.route('/delete_subscription/<type>/<int:id>', methods=['POST'])
@login_required
def delete_subscription(type, id):
    db = get_db()
    if type == 'movie':
        db.execute('DELETE FROM MISS_MOVIES WHERE id = ?', (id,))
    elif type == 'tv':
        db.execute('DELETE FROM MISS_TVS WHERE id = ?', (id,))
    else:
        return "Invalid subscription type", 400
    db.commit()
    return redirect(url_for('subscriptions'))

# 获取豆瓣想看数据的JSON接口
@app.route('/douban_subscriptions_json')
@login_required
def douban_subscriptions_json():
    """
    以JSON格式返回豆瓣订阅数据，供前端调用
    """
    try:
        db = get_db()
        
        # 获取电影订阅数据
        rss_movies = db.execute('SELECT * FROM RSS_MOVIES').fetchall()
        # 获取电视剧订阅数据
        rss_tvs = db.execute('SELECT * FROM RSS_TVS').fetchall()
        
        # 转换为字典列表并添加状态字段
        movies_data = []
        for movie in rss_movies:
            movie_dict = dict(movie)
            # 确保包含 STATUS 字段，默认为 "想看"
            movie_dict['STATUS'] = movie_dict.get('STATUS', '想看')
            movies_data.append(movie_dict)
            
        tvs_data = []
        for tv in rss_tvs:
            tv_dict = dict(tv)
            # 确保包含 STATUS 字段，默认为 "想看"
            tv_dict['STATUS'] = tv_dict.get('STATUS', '想看')
            tvs_data.append(tv_dict)
        
        # 返回JSON响应
        return jsonify({
            "rss_movies": movies_data,
            "rss_tvs": tvs_data
        })
    except Exception as e:
        logger.error(f"获取豆瓣订阅数据失败: {e}")
        return jsonify({"error": "获取数据失败"}), 500

# 获取剧集关联列表的JSON接口
@app.route('/tv_alias_list_json')
@login_required
def tv_alias_list_json():
    try:
        db = get_db()
        alias_list = db.execute('SELECT * FROM LIB_TV_ALIAS ORDER BY id DESC').fetchall()
        # 将Row对象转换为字典列表
        alias_list_dict = [dict(row) for row in alias_list]
        return jsonify({"alias_list": alias_list_dict})
    except Exception as e:
        logger.error(f"获取剧集关联列表失败: {e}")
        return jsonify({"error": "获取剧集关联列表失败"}), 500

# 获取单个剧集关联信息的JSON接口
@app.route('/tv_alias_edit_json/<int:alias_id>')
@login_required
def tv_alias_edit_json(alias_id):
    try:
        db = get_db()
        alias = db.execute('SELECT * FROM LIB_TV_ALIAS WHERE id = ?', (alias_id,)).fetchone()
        if alias:
            return jsonify({"alias": dict(alias)})
        else:
            return jsonify({"error": "未找到该关联"}), 404
    except Exception as e:
        logger.error(f"获取剧集关联信息失败: {e}")
        return jsonify({"error": "获取剧集关联信息失败"}), 500

# 添加剧集关联的API接口
@app.route('/tv_alias_add', methods=['POST'])
@login_required
def tv_alias_add_api():
    try:
        data = request.json
        alias = data.get('alias', '').strip()
        target_title = data.get('target_title', '').strip()
        target_season = data.get('target_season', None)
        
        if not alias or not target_title:
            return jsonify({"success": False, "message": "别名和目标名称不能为空"}), 400
            
        db = get_db()
        try:
            db.execute('INSERT INTO LIB_TV_ALIAS (ALIAS, TARGET_TITLE, TARGET_SEASON) VALUES (?, ?, ?)', 
                      (alias, target_title, target_season))
            db.commit()
            return jsonify({"success": True, "message": "添加成功"})
        except sqlite3.IntegrityError:
            return jsonify({"success": False, "message": "该别名已存在"}), 400
    except Exception as e:
        logger.error(f"添加剧集关联失败: {e}")
        return jsonify({"success": False, "message": "添加失败，请稍后再试"}), 500

# 编辑剧集关联的API接口
@app.route('/tv_alias_edit/<int:alias_id>', methods=['POST'])
@login_required
def tv_alias_edit_api(alias_id):
    try:
        data = request.json
        alias = data.get('alias', '').strip()
        target_title = data.get('target_title', '').strip()
        target_season = data.get('target_season', None)
        
        if not alias or not target_title:
            return jsonify({"success": False, "message": "别名和目标名称不能为空"}), 400
            
        db = get_db()
        existing_alias = db.execute('SELECT * FROM LIB_TV_ALIAS WHERE id = ?', (alias_id,)).fetchone()
        if not existing_alias:
            return jsonify({"success": False, "message": "未找到该关联"}), 404
            
        try:
            db.execute('UPDATE LIB_TV_ALIAS SET ALIAS = ?, TARGET_TITLE = ?, TARGET_SEASON = ? WHERE id = ?', 
                      (alias, target_title, target_season, alias_id))
            db.commit()
            return jsonify({"success": True, "message": "更新成功"})
        except sqlite3.IntegrityError:
            return jsonify({"success": False, "message": "该别名已存在"}), 400
    except Exception as e:
        logger.error(f"更新剧集关联失败: {e}")
        return jsonify({"success": False, "message": "更新失败，请稍后再试"}), 500

# 删除剧集关联的API接口
@app.route('/tv_alias_delete/<int:alias_id>', methods=['POST'])
@login_required
def tv_alias_delete_api(alias_id):
    try:
        db = get_db()
        existing_alias = db.execute('SELECT * FROM LIB_TV_ALIAS WHERE id = ?', (alias_id,)).fetchone()
        if not existing_alias:
            return jsonify({"success": False, "message": "未找到该关联"}), 404
            
        db.execute('DELETE FROM LIB_TV_ALIAS WHERE id = ?', (alias_id,))
        db.commit()
        return jsonify({"success": True, "message": "删除成功"})
    except Exception as e:
        logger.error(f"删除剧集关联失败: {e}")
        return jsonify({"success": False, "message": "删除失败，请稍后再试"}), 500

@app.route('/service_control')
@login_required
def service_control():
    # 从会话中获取用户昵称和头像
    nickname = session.get('nickname')
    avatar_url = session.get('avatar_url')
    return render_template('service_control.html', nickname=nickname, avatar_url=avatar_url, version=APP_VERSION)

@app.route('/run_service', methods=['POST'])
@login_required
def run_service():
    data = request.get_json()
    service = data.get('service')
    try:
        logger.info(f"尝试启动服务: {service}")
        log_file_path = f'/tmp/log/{service}.log'
        os.makedirs(os.path.dirname(log_file_path), exist_ok=True)  # 确保日志目录存在
        with open(log_file_path, 'w', encoding='utf-8') as log_file:
            process = subprocess.Popen(['python3', f'/app/{service}.py'], stdout=log_file, stderr=log_file)
            pid = process.pid
            running_services[service] = pid
        logger.info(f"服务 {service} 启动成功，PID: {pid}")
        return jsonify({"message": "服务运行成功！", "pid": pid}), 200
    except Exception as e:
        logger.error(f"服务 {service} 启动失败: {e}")
        return jsonify({"message": str(e)}), 500

@app.route('/realtime_log/<string:service>')
@login_required
def realtime_log(service):
    @stream_with_context
    def generate():
        log_file_path = f'/tmp/log/{service}.log'
        if not os.path.exists(log_file_path):
            logger.warning(f"实时日志文件不存在: {log_file_path}")
            yield 'data: 当前没有实时运行日志，请检查服务是否正在运行！\n\n'.encode('utf-8')
            return
        
        # 检查文件是否为空
        if os.path.getsize(log_file_path) == 0:
            logger.warning(f"实时日志文件为空: {log_file_path}")
            yield 'data: 当前日志文件为空\n\n'.encode('utf-8')
            return

        logger.info(f"开始读取实时日志: {log_file_path}")
        with open(log_file_path, 'r', encoding='utf-8') as log_file:
            while True:
                line = log_file.readline()
                if not line:
                    time.sleep(0.1)
                    # 检查是否需要停止日志传输
                    if not log_streaming_status.get(service, True):
                        logger.info(f"停止读取日志: {log_file_path}")
                        break
                    continue
                yield f'data: {line}\n\n'
    log_streaming_status[service] = True  # 初始化日志传输状态为 True
    return Response(generate(), mimetype='text/event-stream', content_type='text/event-stream; charset=utf-8')

@app.route('/stop_realtime_log/<string:service>', methods=['POST'])
@login_required
def stop_realtime_log(service):
    try:
        log_streaming_status[service] = False  # 设置日志传输状态为 False
        logger.info(f"停止实时日志传输: {service}")
        return jsonify({"message": "实时日志传输已停止"}), 200
    except Exception as e:
        logger.error(f"停止实时日志传输失败: {e}")
        return jsonify({"message": "停止实时日志传输失败"}), 500

GROUP_MAPPING = {
    "浏览器驱动": {
        "chromedriver_path": {"type": "text", "label": "ChromeDriver 路径（Windows 可填 chromedriver.exe）"}
    },
    "定时任务": {
        "run_interval_hours": {"type": "text", "label": "自动化流程间隔"}
    },
    "消息通知": {
        "notification": {"type": "switch", "label": "消息通知总开关"},
        "bark_enabled": {"type": "switch", "label": "Bark 推送"},
        "notification_api_key": {"type": "password", "label": "Bark API密钥"},
        "dingtalk_enabled": {"type": "switch", "label": "钉钉推送"},
        "dingtalk_webhook": {"type": "password", "label": "钉钉机器人Webhook"},
        "dingtalk_secret": {"type": "password", "label": "钉钉加签密钥"},
    },
    "媒体添加时间": {
        "dateadded": {"type": "switch", "label": "发行日期作为媒体添加日期"}
    },
    "媒体元数据刮削": {
        "scrape_metadata": {"type": "switch", "label": "刮削媒体元数据"},
        "scrape_plot": {"type": "switch", "label": "刮削简介"},
        "scrape_actors": {"type": "switch", "label": "刮削演员信息"},
        "scrape_director": {"type": "switch", "label": "刮削导演信息"},
        "scrape_actor_thumb": {"type": "switch", "label": "刮削演职人员头像"},
        "scrape_ratings": {"type": "switch", "label": "刮削评分信息"},
        "scrape_genres": {"type": "switch", "label": "刮削类型信息"},
        "scrape_tags": {"type": "switch", "label": "刮削标签信息"},
        "scrape_studios": {"type": "switch", "label": "刮削制片公司信息"},
        "scrape_poster": {"type": "switch", "label": "下载海报图"},
        "scrape_fanart": {"type": "switch", "label": "下载背景图"},
        "scrape_clearlogo": {"type": "switch", "label": "下载Logo图"}
    },
    "中文演职人员": {
        "actor_nfo": {"type": "switch", "label": "演职人员汉化"},
        "nfo_exclude_dirs": {"type": "text", "label": "汉化排除目录"},
        "nfo_excluded_filenames": {"type": "text", "label": "汉化排除文件名"},
        "nfo_excluded_subdir_keywords": {"type": "text", "label": "汉化排除关键字"}
    },
    "媒体库目录": {
        "media_dir": {"type": "text", "label": "主目录"},
        "movies_path": {"type": "text", "label": "电影"},
        "anime_path": {"type": "text", "label": "动漫"},
        "variety_path": {"type": "text", "label": "综艺"},
        "episodes_path": {"type": "text", "label": "电视剧"},
        "unknown_path": {"type": "text", "label": "未识别"}
    },
    "资源下载设置": {
        "preferred_resolution": {"type": "text", "label": "资源下载首选分辨率"},
        "fallback_resolution": {"type": "text", "label": "资源下载备选分辨率"},
        "resources_exclude_keywords": {"type": "text", "label": "资源搜索排除关键词"},
        "resources_prefer_keywords": {"type": "text", "label": "资源下载偏好关键词"}
    },
    "文件转移设置": {
        "download_dir": {"type": "text", "label": "下载监控目录"},
        "download_action": {"type": "select", "label": "入库转移方式", "options": ["move", "copy", "softlink", "hardlink"]},
        "download_excluded_filenames": {"type": "text", "label": "下载转移排除的文件名"},
        "file_overwrite_option": {"type": "select", "label": "文件覆盖选项", "options": ["skip", "size", "always"]},
        "enable_multithread_transfer": {"type": "switch", "label": "启用多线程文件转移"},
        "transfer_thread_count": {"type": "text", "label": "批量文件转移线程数"},
        "movie_folder_naming_format": {"type": "text", "label": "电影目录命名规则"},
        "tv_folder_naming_format": {"type": "text", "label": "电视剧目录命名规则"},
        "anime_folder_naming_format": {"type": "text", "label": "动漫目录命名规则"},
        "variety_folder_naming_format": {"type": "text", "label": "综艺目录命名规则"},
        "movie_naming_format": {"type": "text", "label": "电影文件命名规则"},
        "tv_naming_format": {"type": "text", "label": "电视剧文件命名规则"},
        "anime_naming_format": {"type": "text", "label": "动漫文件命名规则"},
        "variety_naming_format": {"type": "text", "label": "综艺文件命名格式"}        
    },
    "豆瓣设置": {
        "douban_api_key": {"type": "password", "label": "豆瓣API密钥"},
        "douban_cookie": {"type": "text", "label": "豆瓣COOKIE"},
        "douban_user_ids": {"type": "text", "label": "豆瓣订阅用户ID"},
        "douban_rss_url": {"type": "text", "label": "豆瓣订阅地址"}
    },
    "TMDB接口": {
        "tmdb_base_url": {"type": "text", "label": "TMDB API接口地址"},
        "tmdb_api_key": {"type": "password", "label": "TMDB API密钥"}
    },
    "OCR接口": {
        "ocr_api_key": {"type": "password", "label": "OCR API密钥"}
    },
    "TMM设置": {
    "tmm_enabled": {"type": "switch", "label": "启用 TMM 集成"},
    "tmm_api_url": {"type": "text", "label": "TMM API 地址"},
    "tmm_api_key": {"type": "password", "label": "TMM API 密钥"}
    },
    "下载器管理": {
        "download_mgmt": {"type": "switch", "label": "下载器管理"},
        "download_type": {"type": "downloader", "label": "下载器", "options": ["xunlei"]},
        "download_username": {"type": "text", "label": "下载器用户名"},
        "download_password": {"type": "password", "label": "下载器密码"},
        "download_host": {"type": "text", "label": "下载器地址"},
        "download_port": {"type": "text", "label": "下载器端口"},
        "xunlei_device_name": {"type": "text", "label": "迅雷设备名称"},
        "xunlei_dir": {"type": "text", "label": "迅雷下载目录"},
        "xunlei_vendor": {"type": "select", "label": "定制厂商", "options": {"": "通用", "ugreen": "绿联", "lenovo": "联想", "unibox": "UniBOX", "hik": "海康威视", "lex": "雷克沙", "jkj": "极空间", "huawei": "华为"}}
    },
    "站点索引开关": {
        "btys_enabled": {"type": "switch", "label": "BT影视"},
        "bt0_enabled": {"type": "switch", "label": "不太灵影视"},
        "gy_enabled": {"type": "switch", "label": "观影"},
        "1lou_enabled": {"type": "switch", "label": "BT之家(1LOU)"},
        "jackett_enabled": {"type": "switch", "label": "Jackett"}
    },
    "Jackett 设置": {
        "jackett_base_url": {"type": "text", "label": "Jackett 地址（如 http://127.0.0.1:9117）"},
        "jackett_api_key": {"type": "password", "label": "Jackett API Key"},
        "jackett_verify_ssl": {"type": "switch", "label": "验证 SSL 证书（https，若反代证书异常可关闭）"},
        "jackett_timeout_seconds": {"type": "text", "label": "Jackett 超时秒数（read timeout，建议 60-120）"},
        "jackett_retries": {"type": "text", "label": "Jackett 重试次数（超时/错误时）"}
    },
    "私有资源站点设置": {
        "bt_login_username": {"type": "text", "label": "站点登录用户名"},
        "bt_login_password": {"type": "password", "label": "站点登录密码"}
    },
    "公开资源站点设置": {
        "bt0_login_username": {"type": "text", "label": "不太灵影视登录用户名"},
        "bt0_login_password": {"type": "password", "label": "不太灵影视登录密码"},
        "gy_login_username": {"type": "text", "label": "观影登录用户名"},
        "gy_login_password": {"type": "password", "label": "观影登录密码"},
        "btys_base_url": {"type": "text", "label": "BT影视"},
        "bt0_base_url": {"type": "text", "label": "不太灵影视"},
        "gy_base_url": {"type": "text", "label": "观影"},
        "1lou_base_url": {"type": "text", "label": "BT之家(1LOU)"},
        "1lou_ok1_cookie": {"type": "text", "label": "BT之家(1LOU)ok1_cookie"},
        "1lou_max_hits": {"type": "text", "label": "BT之家(1LOU) 最多合并帖子数"}
    }
}

@app.route('/settings')
@login_required
def settings_page():
    # 从数据库读取配置项（包括 ID 字段）
    db = get_db()
    config_rows = db.execute('SELECT ID, OPTION, VALUE FROM CONFIG').fetchall()

    # 将配置项转换为新的分组数据结构
    grouped_config_data = {}
    for row in config_rows:
        option_id = row['ID']  # 获取 ID 字段
        option = row['OPTION']
        value = row['VALUE']

        # 遍历分组映射，找到对应的分组
        for group_name, group_items in GROUP_MAPPING.items():
            if option in group_items:
                if group_name not in grouped_config_data:
                    grouped_config_data[group_name] = {}
                grouped_config_data[group_name][option] = {
                    "id": option_id,  # 添加 ID 字段
                    "value": value,
                    **group_items[option]  # 合并类型和标签信息
                }
                break

    # 确保 "定时任务" 始终是最后一项
    if "定时任务" in grouped_config_data:
        timed_task = grouped_config_data.pop("定时任务")
        grouped_config_data["定时任务"] = timed_task

    # 从会话中获取用户昵称和头像
    nickname = session.get('nickname')
    avatar_url = session.get('avatar_url')

    # 渲染模板并传递分组后的配置数据
    return render_template('settings.html', config=grouped_config_data, nickname=nickname, avatar_url=avatar_url, version=APP_VERSION)

@app.route('/save_set', methods=['POST'])
@login_required
def save_settings():
    db = get_db()
    form_data = request.form
    try:
        for key, value in form_data.items():
            if not key.endswith('_id'):
                option_id = form_data.get(f"{key}_id")
                if option_id:
                    logger.info(f"更新配置项 ID={option_id}, KEY={key}, VALUE={value}")
                    db.execute('UPDATE CONFIG SET VALUE = ? WHERE ID = ?', (value, option_id))
        db.commit()
        logger.info("配置保存成功")
        flash('设置已成功保存！', 'success')
    except Exception as e:
        db.rollback()
        logger.error(f"配置保存失败: {e}")
        flash('设置保存失败，请稍后再试。', 'error')
    return redirect(url_for('settings_page'))

@app.route('/api/browse_directory', methods=['GET'])
@login_required
def browse_directory():
    """
    浏览目录结构的API接口
    """
    path = request.args.get('path', '/')
    try:
        # 安全检查，确保路径在允许的范围内
        if path == '/':
            # 允许访问根目录下的所有路径
            items = []
            try:
                for item in os.listdir(path):
                    item_path = os.path.join(path, item)
                    # 只显示目录
                    if os.path.isdir(item_path):
                        items.append({
                            'name': item,
                            'path': item_path,
                            'is_dir': True
                        })
            except PermissionError:
                return jsonify({'error': '没有权限访问根目录'}), 403
                
            # 按名称排序
            items.sort(key=lambda x: x['name'].lower())
            return jsonify({'path': path, 'items': items})
        
        # 确保路径存在且为目录
        if not os.path.exists(path) or not os.path.isdir(path):
            return jsonify({'error': '路径不存在或不是目录'}), 400
            
        items = []
        try:
            for item in os.listdir(path):
                item_path = os.path.join(path, item)
                items.append({
                    'name': item,
                    'path': item_path,
                    'is_dir': os.path.isdir(item_path)
                })
        except PermissionError:
            return jsonify({'error': '没有权限访问该目录'}), 403
            
        # 按目录和名称排序
        items.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
        
        # 添加上级目录
        parent_path = os.path.dirname(path)
        if parent_path != path:  # 不是根目录
            items.insert(0, {
                'name': '..',
                'path': parent_path,
                'is_dir': True
            })
            
        return jsonify({'path': path, 'items': items})
    except Exception as e:
        logger.error(f"浏览目录失败: {e}")
        return jsonify({'error': '浏览目录失败'}), 500

@app.route('/api/create_directory', methods=['POST'])
@login_required
def create_directory():
    """
    创建新目录的API接口
    """
    try:
        data = request.json
        parent_path = data.get('path')
        dir_name = data.get('dir_name')
        
        if not parent_path or not dir_name:
            return jsonify({'error': '缺少必要参数'}), 400
            
        # 防止目录遍历攻击
        if '..' in dir_name or dir_name.startswith('/'):
            return jsonify({'error': '无效的目录名称'}), 400
            
        new_dir_path = os.path.join(parent_path, dir_name)
        
        # 检查目录是否已存在
        if os.path.exists(new_dir_path):
            return jsonify({'error': '目录已存在'}), 400
            
        # 创建目录
        os.makedirs(new_dir_path, exist_ok=True)
        logger.info(f"成功创建目录: {new_dir_path}")
        
        # 返回新建目录的完整路径
        return jsonify({'message': '目录创建成功', 'path': new_dir_path}), 200
    except Exception as e:
        logger.error(f"创建目录失败: {e}")
        return jsonify({'error': '创建目录失败'}), 500

@app.route('/api/rename_directory', methods=['POST'])
@login_required
def rename_directory():
    """
    重命名目录的API接口
    """
    try:
        data = request.json
        old_path = data.get('old_path')
        new_name = data.get('new_name')
        
        if not old_path or not new_name:
            return jsonify({'error': '缺少必要参数'}), 400
            
        # 防止目录遍历攻击
        if '..' in new_name or new_name.startswith('/'):
            return jsonify({'error': '无效的目录名称'}), 400
            
        # 确保原路径存在且为目录
        if not os.path.exists(old_path) or not os.path.isdir(old_path):
            return jsonify({'error': '原目录不存在或不是目录'}), 400
            
        # 构造新路径
        parent_path = os.path.dirname(old_path)
        new_path = os.path.join(parent_path, new_name)
        
        # 检查新路径是否已存在
        if os.path.exists(new_path):
            return jsonify({'error': '目标目录已存在'}), 400
            
        # 重命名目录
        os.rename(old_path, new_path)
        logger.info(f"成功重命名目录: {old_path} -> {new_path}")
        
        return jsonify({'message': '目录重命名成功', 'path': new_path}), 200
    except Exception as e:
        logger.error(f"重命名目录失败: {e}")
        return jsonify({'error': '重命名目录失败'}), 500

@app.route('/download_mgmt')
@login_required
def download_mgmt_page():
    db = get_db()
    
    # 从数据库中读取 download_mgmt 的配置
    download_mgmt_config = db.execute('SELECT VALUE FROM CONFIG WHERE OPTION = ?', ('download_mgmt',)).fetchone()
    
    # 检查 download_mgmt 是否存在且为 True
    if not download_mgmt_config or download_mgmt_config['VALUE'] != 'True':
        flash('下载管理功能未启用，请在系统设置中开启下载管理功能。', 'error')
        return redirect(url_for('settings_page'))
    
    # 获取 download_type 配置
    download_type_config = db.execute('SELECT VALUE FROM CONFIG WHERE OPTION = ?', ('download_type',)).fetchone()
    download_type = download_type_config['VALUE'] if download_type_config else None
    
    # 获取 delete_with_files 配置
    delete_with_files_config = db.execute('SELECT VALUE FROM CONFIG WHERE OPTION = ?', ('delete_with_files',)).fetchone()
    if not delete_with_files_config:
        # 如果配置项不存在，创建默认配置
        db.execute('INSERT INTO CONFIG (OPTION, VALUE) VALUES (?, ?)', ('delete_with_files', 'False'))
        db.commit()
        delete_with_files = False
    else:
        delete_with_files = delete_with_files_config['VALUE'] == 'True'
        
    # 获取 auto_delete_completed_tasks 配置
    auto_delete_completed_tasks_config = db.execute('SELECT VALUE FROM CONFIG WHERE OPTION = ?', ('auto_delete_completed_tasks',)).fetchone()
    if not auto_delete_completed_tasks_config:
        # 如果配置项不存在，创建默认配置
        db.execute('INSERT INTO CONFIG (OPTION, VALUE) VALUES (?, ?)', ('auto_delete_completed_tasks', 'False'))
        db.commit()
        auto_delete_completed_tasks = False
    else:
        auto_delete_completed_tasks = auto_delete_completed_tasks_config['VALUE'] == 'True'

    # 从会话中获取用户昵称和头像
    nickname = session.get('nickname')
    avatar_url = session.get('avatar_url')

    # 读取迅雷厂商配置
    xunlei_vendor_config = db.execute('SELECT VALUE FROM CONFIG WHERE OPTION = ?', ('xunlei_vendor',)).fetchone()
    xunlei_vendor = xunlei_vendor_config['VALUE'] if xunlei_vendor_config else ''

    # 根据 download_type 使用不同模板
    if download_type == 'xunlei':
        template_name = 'xunlei.html'
    else:
        template_name = 'download_mgmt.html'

    # 将信息传递给模板
    return render_template(template_name, nickname=nickname, avatar_url=avatar_url, 
                         download_mgmt=download_mgmt_config, delete_with_files=delete_with_files,
                         auto_delete_completed_tasks=auto_delete_completed_tasks, version=APP_VERSION,
                         xunlei_vendor=xunlei_vendor)

# 获取下载器客户端
def get_downloader_client():
    # 仅支持迅雷下载器，迅雷通过 Selenium 远程管理，无需返回客户端实例
    return None

# 获取任务列表
@app.route('/api/download/list', methods=['GET'])
@login_required
def list_torrents():
    # 仅支持迅雷下载器，迅雷任务通过迅雷页面管理，此处返回空列表
    return jsonify({"torrents": []})

@app.route('/api/download/add', methods=['POST'])
@login_required
def add_torrent():
    # 仅支持迅雷下载器，迅雷通过 Selenium 远程添加任务
    return jsonify({"error": "当前仅支持迅雷下载器，请通过迅雷页面添加任务"}), 400

# 批量操作（启动、暂停、删除）的API
@app.route('/api/download/<action>', methods=['POST'])
@login_required
def bulk_action(action):
    # 仅支持迅雷下载器，迅雷通过迅雷页面管理
    return jsonify({"error": "当前仅支持迅雷下载器，批量操作请通过迅雷页面进行"}), 400

# 用于切换 delete_with_files 设置
@app.route('/api/download/toggle_delete_with_files', methods=['POST'])
@login_required
def toggle_delete_with_files():
    try:
        data = request.json
        enabled = data.get("enabled", False)
        
        db = get_db()
        # 更新配置
        db.execute('UPDATE CONFIG SET VALUE = ? WHERE OPTION = ?', 
                  ('True' if enabled else 'False', 'delete_with_files'))
        db.commit()
        
        logger.info(f"删除任务时同时删除本地文件设置已更新为: {enabled}")
        return jsonify({"message": "设置已更新"})
    except Exception as e:
        logger.error(f"更新设置失败: {e}")
        return jsonify({"error": str(e)}), 500

# 用于切换 auto_delete_completed_tasks 设置
@app.route('/api/download/toggle_auto_delete_completed_tasks', methods=['POST'])
@login_required
def toggle_auto_delete_completed_tasks():
    try:
        data = request.json
        enabled = data.get("enabled", False)
        
        db = get_db()
        # 更新配置
        db.execute('UPDATE CONFIG SET VALUE = ? WHERE OPTION = ?', 
                  ('True' if enabled else 'False', 'auto_delete_completed_tasks'))
        db.commit()
        
        logger.info(f"自动删除已完成任务设置已更新为: {enabled}")
        return jsonify({"message": "设置已更新"})
    except Exception as e:
        logger.error(f"更新设置失败: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/download/get-magnet-links', methods=['POST'])
@login_required
def get_magnet_links():
    # 仅支持迅雷下载器，迅雷任务通过迅雷页面管理
    return jsonify({"error": "当前仅支持迅雷下载器，获取磁力链接请通过迅雷页面进行"}), 400

@app.route('/test_downloader_connection', methods=['POST'])
@login_required
def test_downloader_connection():
    """
    测试下载器连接
    """
    # 仅支持迅雷下载器，迅雷通过 Selenium 远程登录测试，无需 HTTP 连接测试
    return jsonify({"success": False, "message": "当前仅支持迅雷下载器，无需测试连接"}), 400

@app.route('/test_bark', methods=['POST'])
@login_required
def test_bark():
    """测试 Bark 推送"""
    try:
        data = request.json or {}
        api_key = (data.get('api_key') or '').strip()
        if not api_key:
            db = get_db()
            row = db.execute("SELECT VALUE FROM CONFIG WHERE OPTION = 'notification_api_key'").fetchone()
            api_key = (row['VALUE'] if row else '').strip()
        if not api_key:
            return jsonify({"success": False, "message": "未配置 Bark API 密钥"})
        api_url = f"https://api.day.app/{api_key}"
        payload = {"title": "MediaMaster 测试", "body": "Bark 推送测试成功！"}
        resp = requests.post(api_url, data=json.dumps(payload), headers={'Content-Type': 'application/json'}, timeout=10)
        if resp.status_code == 200:
            return jsonify({"success": True, "message": "Bark 推送测试成功！"})
        return jsonify({"success": False, "message": f"Bark 推送失败: {resp.status_code} {resp.text}"})
    except Exception as e:
        logger.error(f"Bark 测试失败: {e}")
        return jsonify({"success": False, "message": f"Bark 测试异常: {e}"})

@app.route('/test_dingtalk', methods=['POST'])
@login_required
def test_dingtalk():
    """测试钉钉推送"""
    try:
        data = request.json or {}
        webhook = (data.get('webhook') or '').strip()
        secret = (data.get('secret') or '').strip()
        if not webhook:
            db = get_db()
            row = db.execute("SELECT VALUE FROM CONFIG WHERE OPTION = 'dingtalk_webhook'").fetchone()
            webhook = (row['VALUE'] if row else '').strip()
            row = db.execute("SELECT VALUE FROM CONFIG WHERE OPTION = 'dingtalk_secret'").fetchone()
            secret = (row['VALUE'] if row else '').strip()
        if not webhook:
            return jsonify({"success": False, "message": "未配置钉钉 Webhook"})

        # 加签
        import time as _time
        import hashlib as _hashlib
        import hmac as _hmac
        import base64 as _base64
        import urllib.parse as _uparse
        if secret:
            timestamp = str(round(_time.time() * 1000))
            string_to_sign = f"{timestamp}\n{secret}"
            hmac_code = _hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=_hashlib.sha256).digest()
            sign = _uparse.quote_plus(_base64.b64encode(hmac_code))
            webhook = f"{webhook}&timestamp={timestamp}&sign={sign}"

        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": "📢 MediaMaster 测试",
                "text": "### 📢 MediaMaster 测试\n\n钉钉推送测试成功！"
            }
        }
        resp = requests.post(webhook, data=json.dumps(payload), headers={'Content-Type': 'application/json'}, timeout=10)
        resp_json = resp.json() if resp.status_code == 200 else {}
        if resp.status_code == 200 and resp_json.get("errcode") == 0:
            return jsonify({"success": True, "message": "钉钉推送测试成功！"})
        return jsonify({"success": False, "message": f"钉钉推送失败: {resp.status_code} {resp.text}"})
    except Exception as e:
        logger.error(f"钉钉测试失败: {e}")
        return jsonify({"success": False, "message": f"钉钉测试异常: {e}"})

@app.route('/test_tmm_connection', methods=['POST'])
@login_required
def test_tmm_connection():
    """
    测试TMM连接功能
    """
    try:
        data = request.json
        tmm_api_url = data.get('tmm_api_url')
        tmm_api_key = data.get('tmm_api_key')
        
        if not tmm_api_url or not tmm_api_key:
            return jsonify({"success": False, "message": "缺少必要的参数"}), 400
            
        # 确保URL以/结尾
        if not tmm_api_url.endswith('/'):
            tmm_api_url += '/'
            
        # 使用电影API端点进行测试
        test_url = f"{tmm_api_url}api/movies"
        
        # 准备测试数据，使用minimal操作以减少资源消耗
        test_payload = [
            {"action": "update", "scope": {"name": "all"}}
        ]
        
        # 发送请求测试连接
        headers = {
            'Content-Type': 'application/json',
            'api-key': tmm_api_key
        }
        
        # 发送POST请求测试连接
        response = requests.post(test_url, json=test_payload, headers=headers, timeout=10)
        
        # 检查响应状态码来判断连接是否成功
        if response.status_code in [200, 202, 204]:
            return jsonify({
                "success": True, 
                "message": "连接成功"
            })
        elif response.status_code == 401:
            return jsonify({
                "success": False, 
                "message": "认证失败，请检查API密钥是否正确"
            }), 400
        elif response.status_code == 404:
            return jsonify({
                "success": False, 
                "message": "API端点未找到，请检查TMM API地址是否正确"
            }), 400
        else:
            # 返回详细错误信息帮助调试
            error_detail = response.text if response.text else f"HTTP状态码: {response.status_code}"
            return jsonify({
                "success": False, 
                "message": f"连接失败: {error_detail}"
            }), 400
            
    except requests.exceptions.Timeout:
        return jsonify({
            "success": False, 
            "message": "连接超时，请检查网络和URL配置"
        }), 400
    except requests.exceptions.ConnectionError:
        return jsonify({
            "success": False, 
            "message": "连接错误，请检查URL配置和网络连接"
        }), 400
    except Exception as e:
        logger.error(f"TMM连接测试失败: {e}")
        return jsonify({
            "success": False, 
            "message": f"测试过程中发生错误: {str(e)}"
        }), 500

@app.route('/test_jackett_connection', methods=['POST'])
@login_required
def test_jackett_connection():
    """
    测试 Jackett 连接（Torznab API）

    前端会传入 base_url/api_key/timeout_seconds/retries。
    这里做最小化的 torznab 查询来验证：网络/证书/反代/密钥/响应格式。
    """
    try:
        data = request.json or {}
        base_url = (data.get('jackett_base_url') or '').strip()
        api_key = (data.get('jackett_api_key') or '').strip()

        try:
            timeout_seconds = int(float(data.get('jackett_timeout_seconds') or 90))
        except Exception:
            timeout_seconds = 90

        try:
            retries = int(float(data.get('jackett_retries') or 2))
        except Exception:
            retries = 2

        verify_ssl_raw = data.get('jackett_verify_ssl')
        if isinstance(verify_ssl_raw, bool):
            verify_ssl = verify_ssl_raw
        else:
            verify_ssl = str(verify_ssl_raw or 'True').strip().lower() == 'true'

        timeout_seconds = max(5, min(timeout_seconds, 300))
        retries = max(0, min(retries, 5))

        if not base_url or not api_key:
            return jsonify({"success": False, "message": "请填写 Jackett 地址和 API Key"}), 400

        if not base_url.endswith('/'):
            base_url += '/'

        torznab_url = f"{base_url}api/v2.0/indexers/all/results/torznab/api"
        params = {
            'apikey': api_key,
            't': 'search',
            'q': 'mediamaster',
            'limit': 1,
        }

        start = time.monotonic()
        last_error = None
        for attempt in range(retries + 1):
            try:
                response = requests.get(
                    torznab_url,
                    params=params,
                    timeout=(10, timeout_seconds),
                    verify=verify_ssl,
                )
                elapsed_ms = int((time.monotonic() - start) * 1000)

                if response.status_code == 401:
                    return jsonify({
                        "success": False,
                        "message": "认证失败（401），请检查 API Key 是否正确",
                        "elapsed_ms": elapsed_ms,
                    }), 400
                if response.status_code == 404:
                    return jsonify({
                        "success": False,
                        "message": "Torznab API 端点未找到（404），请检查 Jackett 地址/反代路径",
                        "elapsed_ms": elapsed_ms,
                    }), 400
                if response.status_code >= 400:
                    return jsonify({
                        "success": False,
                        "message": f"HTTP 错误：{response.status_code}",
                        "elapsed_ms": elapsed_ms,
                    }), 400

                # 尝试解析 XML，确认不是 HTML 错误页
                body = (response.text or '').strip()
                if not body:
                    return jsonify({
                        "success": False,
                        "message": "响应为空，请检查 Jackett 服务状态",
                        "elapsed_ms": elapsed_ms,
                    }), 400

                try:
                    import xml.etree.ElementTree as ET

                    ET.fromstring(body)
                except Exception:
                    snippet = body[:200].replace('\n', ' ')
                    return jsonify({
                        "success": False,
                        "message": f"响应不是有效的 XML（可能是反代/鉴权页/错误页）：{snippet}",
                        "elapsed_ms": elapsed_ms,
                    }), 400

                return jsonify({
                    "success": True,
                    "message": "连接成功",
                    "elapsed_ms": elapsed_ms,
                })
            except requests.exceptions.Timeout as e:
                last_error = f"连接超时（read-timeout={timeout_seconds}s）"
                if attempt < retries:
                    time.sleep(min(2 ** attempt, 5))
                    continue
                elapsed_ms = int((time.monotonic() - start) * 1000)
                return jsonify({
                    "success": False,
                    "message": last_error,
                    "elapsed_ms": elapsed_ms,
                }), 400
            except requests.exceptions.ConnectionError as e:
                last_error = "连接错误，请检查 Jackett 地址/网络/证书"
                if attempt < retries:
                    time.sleep(min(2 ** attempt, 5))
                    continue
                elapsed_ms = int((time.monotonic() - start) * 1000)
                return jsonify({
                    "success": False,
                    "message": last_error,
                    "elapsed_ms": elapsed_ms,
                }), 400
            except Exception as e:
                last_error = f"测试过程中发生错误: {str(e)}"
                if attempt < retries:
                    time.sleep(min(2 ** attempt, 5))
                    continue
                elapsed_ms = int((time.monotonic() - start) * 1000)
                logger.error(f"Jackett连接测试失败: {e}")
                return jsonify({
                    "success": False,
                    "message": last_error,
                    "elapsed_ms": elapsed_ms,
                }), 500

        # 理论不会到这里
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return jsonify({
            "success": False,
            "message": last_error or "未知错误",
            "elapsed_ms": elapsed_ms,
        }), 400

    except Exception as e:
        logger.error(f"Jackett连接测试失败(outer): {e}")
        return jsonify({
            "success": False,
            "message": f"测试过程中发生错误: {str(e)}",
        }), 500

def compare_versions(current, latest):
    """比较版本号，返回是否需要更新"""
    current_parts = list(map(int, current.split('.')))
    latest_parts = list(map(int, latest.split('.')))
    return latest_parts > current_parts

@app.route('/health_check', methods=['GET'])
def health_check():
    return jsonify({"status": "ok"}), 200

@app.route('/restart_program', methods=['POST'])
@login_required
def restart_program():
    """
    重启程序：结束主进程以触发自动重启
    """
    try:
        logger.info("开始执行程序重启操作")
        
        # 检查是否有重启权限
        if not session.get('user_id'):
            logger.warning("未授权用户尝试执行重启")
            return jsonify({"error": "未授权的操作"}), 403

        logger.info("准备重启程序，正在结束主进程...")
        
        # 异步重启容器
        def restart_container():
            logger.info("正在重启容器...")
            time.sleep(2)
            # 查找并结束主进程
            target_process_name = "main.py"
            found_process = False

            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    # 检查进程是否运行了 main.py
                    if target_process_name in proc.info['cmdline']:
                        logger.info(f"找到目标进程: PID={proc.info['pid']}, CMD={proc.info['cmdline']}")
                        proc.terminate()  # 发送终止信号
                        proc.wait(timeout=5)  # 等待进程结束
                        found_process = True
                        logger.info(f"已成功结束进程: PID={proc.info['pid']}")
                except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                    continue

            if not found_process:
                logger.warning("未找到运行中的 python main.py 进程")
        
        # 启动后台线程执行重启操作
        threading.Thread(target=restart_container).start()
        
        return jsonify({"message": "重启命令已发送！程序将自动重启。"}), 200
        
    except Exception as e:
        logger.error(f"重启程序失败: {e}")
        return jsonify({"error": "重启失败，请稍后再试。"}), 500

@app.route('/reset_program', methods=['POST'])
@login_required
def reset_program():
    """
    重置程序：删除/config目录中的所有文件并重启容器，但保留client_id文件
    """
    try:
        logger.info("开始执行程序重置操作")
        
        # 检查是否有重置权限
        if not session.get('user_id'):
            logger.warning("未授权用户尝试执行重置")
            return jsonify({"error": "未授权的操作"}), 403

        # 删除/config目录中的所有文件和子目录，但保留/config目录本身和client_id文件
        config_dir = '/config'
        if os.path.exists(config_dir):
            for item in os.listdir(config_dir):
                # 跳过client_id文件
                if item == 'client_id':
                    logger.info("保留client_id文件")
                    continue
                    
                item_path = os.path.join(config_dir, item)
                if os.path.isfile(item_path) or os.path.islink(item_path):
                    os.unlink(item_path)
                    logger.info(f"已删除文件: {item_path}")
                elif os.path.isdir(item_path):
                    import shutil
                    shutil.rmtree(item_path)
                    logger.info(f"已删除目录: {item_path}")
        
        logger.info("配置文件已清理完成，准备重启容器")
        
        # 异步重启容器
        def restart_container():
            logger.info("正在重启容器...")
            time.sleep(2)
            # 查找并结束主进程
            target_process_name = "main.py"
            found_process = False

            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    # 检查进程是否运行了 main.py
                    if target_process_name in proc.info['cmdline']:
                        logger.info(f"找到目标进程: PID={proc.info['pid']}, CMD={proc.info['cmdline']}")
                        proc.terminate()  # 发送终止信号
                        proc.wait(timeout=5)  # 等待进程结束
                        found_process = True
                        logger.info(f"已成功结束进程: PID={proc.info['pid']}")
                except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                    continue

            if not found_process:
                logger.warning("未找到运行中的 python main.py 进程")
        
        # 启动后台线程执行重启操作
        threading.Thread(target=restart_container).start()
        
        return jsonify({"message": "重置成功！程序将重启以恢复默认配置。"}), 200
        
    except Exception as e:
        logger.error(f"重置程序失败: {e}")
        return jsonify({"error": "重置失败，请稍后再试。"}), 500

@app.route('/check_update', methods=['GET'])
@login_required
def check_update():
    try:
        # 当前版本号
        current_version = APP_VERSION

        # 读取 GITHUB_TOKEN 环境变量,用于 API 鉴权(提升速率限制 60→5000/小时)
        github_token = os.environ.get('GITHUB_TOKEN', '').strip()
        api_headers = {}
        if github_token:
            api_headers['Authorization'] = f'token {github_token}'

        # GitHub API 地址和代理地址
        repo_url = "https://api.github.com/repos/KK-325/mediamaster-v2/releases"
        latest_release_url = "https://api.github.com/repos/KK-325/mediamaster-v2/releases/latest"
        proxy_url = "https://gh.llkk.cc/https://api.github.com/repos/KK-325/mediamaster-v2/releases"
        proxy_latest_url = "https://gh.llkk.cc/https://api.github.com/repos/KK-325/mediamaster-v2/releases/latest"

        # 获取所有发布版本
        try:
            response = requests.get(repo_url, headers=api_headers, timeout=5)
            if response.status_code != 200:
                raise Exception(f"主地址返回异常: {response.text}")
        except Exception as e:
            logger.warning(f"主地址连接失败，尝试代理: {e}")
            try:
                response = requests.get(proxy_url, headers=api_headers, timeout=8)
            except Exception as e2:
                logger.error(f"代理也失败: {e2}")
                # 最终 fallback: 直接下载 versions 文件比对版本号
                try:
                    versions_url = "https://gh.llkk.cc/https://raw.githubusercontent.com/KK-325/mediamaster-v2/main/versions"
                    raw_response = requests.get(versions_url, timeout=8)
                    if raw_response.status_code == 200:
                        latest_version = raw_response.text.strip()
                        stable_update_available = compare_versions(current_version, latest_version)
                        return jsonify({
                            "current_version": current_version,
                            "latest_stable_version": latest_version,
                            "stable_release_notes": f"在线升级到 {latest_version}",
                            "stable_update_available": stable_update_available,
                            "latest_prerelease_version": None,
                            "prerelease_release_notes": None,
                            "prerelease_update_available": False,
                        })
                    return jsonify({"error": "无法连接到 GitHub，请稍后再试。"}), 500
                except Exception as e3:
                    logger.error(f"versions 文件下载也失败: {e3}")
                    return jsonify({"error": "无法连接到 GitHub，请稍后再试。"}), 500

        releases = response.json()
        
        # 获取GitHub标记的最新稳定版本
        latest_stable_release = None
        try:
            latest_response = requests.get(latest_release_url, headers=api_headers, timeout=5)
            if latest_response.status_code == 200:
                latest_stable_release = latest_response.json()
        except Exception as e:
            logger.warning(f"获取latest release失败，尝试代理: {e}")
            try:
                latest_response = requests.get(proxy_latest_url, headers=api_headers, timeout=8)
                if latest_response.status_code == 200:
                    latest_stable_release = latest_response.json()
            except Exception as e2:
                logger.warning(f"代理获取latest release也失败: {e2}")

        # 如果无法获取GitHub标记的latest release，则查找第一个非预发布版本
        if not latest_stable_release:
            for release in releases:
                if not release.get("prerelease"):
                    latest_stable_release = release
                    break
        
        # 获取最新的预发布版本
        latest_prerelease_release = None
        for release in releases:
            if release.get("prerelease"):
                latest_prerelease_release = release
                break

        # 构建返回数据
        result = {
            "current_version": current_version,
        }
        
        # 处理稳定版信息
        if latest_stable_release:
            stable_version = latest_stable_release.get("tag_name", "").lstrip("v")
            result["latest_stable_version"] = stable_version
            result["stable_release_notes"] = latest_stable_release.get("body", "无更新说明")
            result["stable_update_available"] = compare_versions(current_version, stable_version)
        else:
            result["latest_stable_version"] = None
            result["stable_release_notes"] = None
            result["stable_update_available"] = False
            
        # 处理预发布版信息
        if latest_prerelease_release:
            prerelease_version = latest_prerelease_release.get("tag_name", "").lstrip("v")
            result["latest_prerelease_version"] = prerelease_version
            result["prerelease_release_notes"] = latest_prerelease_release.get("body", "无更新说明")
            result["prerelease_update_available"] = compare_versions(current_version, prerelease_version)
        else:
            result["latest_prerelease_version"] = None
            result["prerelease_release_notes"] = None
            result["prerelease_update_available"] = False
            
        # 总体更新可用性（任一版本有更新即为可用）
        result["is_update_available"] = result["stable_update_available"] or result["prerelease_update_available"]

        return jsonify(result)
    except Exception as e:
        logger.error(f"检查更新失败: {e}")
        return jsonify({"error": "检查更新失败，请稍后再试。"}), 500

def get_all_proxies_sorted(original_url):
    """
    测试所有代理站点的响应时间，按速度排序返回代理地址列表
    """
    proxy_sites = [
        "https://github.dpik.top/",
        "https://gitproxy.click/",
        "https://github-proxy.lixxing.top/",
        "https://tvv.tw/"
    ]
    
    response_times = {}
    proxy_urls = {}  # 存储完整的代理URL
    
    # 首先测试所有代理
    for proxy in proxy_sites:
        proxy_url = proxy + original_url
        proxy_urls[proxy] = proxy_url  # 保存完整URL
        try:
            start_time = time.time()
            # 使用 GET 请求
            response = requests.get(proxy_url, timeout=10)
            elapsed_time = time.time() - start_time
            # 更宽松的判断条件，接受 2xx 和 3xx 状态码
            if response.status_code < 400:
                response_times[proxy] = elapsed_time
            else:
                # 即使状态码不是 200，也给予较低优先级而非完全排除
                response_times[proxy] = float('inf')
        except requests.RequestException as e:
            # 即使请求失败，也给予较低优先级而非完全排除
            response_times[proxy] = float('inf')
            logger.warning(f"代理 {proxy} 测试失败: {e}")
    
    # 原始地址作为后备选项
    response_times[original_url] = float('inf')  # 设为最低优先级
    proxy_urls[original_url] = original_url  # 原始URL
    
    # 按响应时间排序，但保留所有代理（包括响应时间为无穷大的）
    sorted_proxies = [proxy for proxy, time in sorted(response_times.items(), key=lambda x: x[1])]
    
    # 返回代理标识符和完整URL的映射
    return [(proxy, proxy_urls[proxy]) for proxy in sorted_proxies]

@app.route('/perform_update', methods=['POST'])
@login_required
def perform_update():
    try:
        # 获取当前版本号
        current_version = APP_VERSION
        
        # 获取更新类型参数（latest 或 prerelease）
        update_type = request.json.get('type', 'latest')  # 默认更新到最新稳定版

        # 检查是否有更新权限
        if not session.get('user_id'):
            logger.warning("未授权用户尝试执行更新")
            return jsonify({"error": "未授权的操作"}), 403

        logger.info(f"开始执行更新操作，更新类型: {update_type}")
        
        # 步骤1: 获取所有代理并按速度排序
        original_url = "https://github.com/KK-325/mediamaster-v2.git"
        proxy_list = get_all_proxies_sorted(original_url)
        
        # 步骤2: 尝试每个代理进行更新
        git_pull_success = False
        last_error = ""
        
        for proxy_identifier, proxy_url in proxy_list:
            try:
                logger.info(f"尝试使用地址: {proxy_url}")
                
                # 如果是 github.com 地址且配置了 GITHUB_TOKEN，注入 token 实现私有仓库鉴权
                effective_url = proxy_url
                github_token = os.environ.get('GITHUB_TOKEN', '').strip()
                if github_token and 'github.com' in proxy_url and '@' not in proxy_url:
                    # 把 https://github.com/xxx/yyy.git 改成 https://x-access-token:TOKEN@github.com/xxx/yyy.git
                    effective_url = proxy_url.replace('https://github.com', f'https://x-access-token:{github_token}@github.com')
                    logger.info("已检测到 GITHUB_TOKEN 环境变量，将使用 token 鉴权访问 fork 仓库")
                
                # 设置 Git 远程仓库地址
                logger.info(f"正在设置 Git 远程仓库地址: {proxy_url if effective_url == proxy_url else proxy_url.split('@')[-1]}")
                set_remote_result = subprocess.run(
                    ['git', 'remote', 'set-url', 'origin', effective_url],
                    capture_output=True,
                    text=True,
                    cwd='/app'
                )
                
                if set_remote_result.returncode != 0:
                    logger.warning(f"设置远程仓库地址失败: {set_remote_result.stderr}")
                    last_error = set_remote_result.stderr
                    continue
                
                # 重置本地更改，确保干净的更新环境
                logger.info("正在放弃本地更改...")
                try:
                    checkout_result = subprocess.run(
                        ['git', 'checkout', '.'],
                        capture_output=True,
                        text=True,
                        cwd='/app',
                        timeout=30
                    )
                except subprocess.TimeoutExpired:
                    logger.warning("放弃本地更改超时（30秒），跳过此步骤")
                    checkout_result = subprocess.run(['echo'], capture_output=True, text=True)
                
                if checkout_result.returncode != 0:
                    logger.warning(f"放弃本地更改失败: {checkout_result.stderr}")
                    last_error = checkout_result.stderr
                    continue
                           
                # 根据更新类型执行不同的更新操作
                if update_type == 'prerelease':
                    # 更新到最新的预发布版本
                    logger.info("正在获取最新的预发布版本标签...")
                    
                    # 先获取所有发布版本信息
                    repo_urls = [
                        "https://api.github.com/repos/KK-325/mediamaster-v2/releases",
                        "https://gh.llkk.cc/https://api.github.com/repos/KK-325/mediamaster-v2/releases"
                    ]
                    
                    releases = None
                    for repo_url in repo_urls:
                        try:
                            response = requests.get(repo_url, timeout=8)
                            if response.status_code == 200:
                                releases = response.json()
                                break
                        except Exception as e:
                            logger.warning(f"获取release信息失败: {e}")
                            continue
                    
                    if not releases:
                        logger.error("无法获取 GitHub 版本信息")
                        last_error = "无法获取 GitHub 版本信息"
                        continue
                    
                    # 查找最新的预发布版本
                    prerelease_version = None
                    prerelease_version_tag = None
                    for release in releases:
                        if release.get('prerelease'):
                            prerelease_version = release
                            prerelease_version_tag = release.get('tag_name')
                            break
                    
                    if not prerelease_version_tag:
                        logger.warning("未找到预发布版本")
                        last_error = "未找到预发布版本"
                        continue
                    
                    logger.info(f"最新的预发布版本标签: {prerelease_version_tag}")
                    
                    # 拉取指定标签的代码
                    logger.info("正在从 Git 仓库拉取最新预发布版本代码...")
                    try:
                        fetch_result = subprocess.run(
                            ['git', 'fetch', '--all'],
                            capture_output=True,
                            text=True,
                            cwd='/app',
                            timeout=60
                        )
                    except subprocess.TimeoutExpired:
                        error_message = "Git fetch 超时（60秒）"
                        logger.error(error_message)
                        last_error = error_message
                        continue
                    
                    if fetch_result.returncode != 0:
                        error_message = f"Git fetch 失败: {fetch_result.stderr}"
                        logger.error(error_message)
                        last_error = fetch_result.stderr
                        continue
                    
                    # 检出特定标签
                    logger.info(f"正在检出预发布版本 {prerelease_version_tag}...")
                    try:
                        checkout_result = subprocess.run(
                            ['git', 'checkout', prerelease_version_tag],
                            capture_output=True,
                            text=True,
                            cwd='/app',
                            timeout=30
                        )
                    except subprocess.TimeoutExpired:
                        error_message = "Git checkout 超时（30秒）"
                        logger.error(error_message)
                        last_error = error_message
                        continue
                    
                    if checkout_result.returncode != 0:
                        error_message = f"Git checkout 失败: {checkout_result.stderr}"
                        logger.error(error_message)
                        last_error = checkout_result.stderr
                        continue
                    
                    # 拉取代码
                    try:
                        pull_result = subprocess.run(
                            ['git', 'pull', 'origin', prerelease_version_tag],
                            capture_output=True,
                            text=True,
                            cwd='/app',
                            timeout=60
                        )
                    except subprocess.TimeoutExpired:
                        last_error = "Git pull 超时（60秒）"
                        logger.warning(f"Git 拉取预发布版本失败: {last_error}")
                        continue
                    
                    if pull_result.returncode == 0:
                        logger.info(f"Git 拉取预发布版本成功: {pull_result.stdout}")
                        git_pull_success = True
                        break
                    else:
                        last_error = pull_result.stderr
                        logger.warning(f"Git 拉取预发布版本失败: {last_error}")
                else:
                    # 更新到最新稳定版本（默认行为）
                    logger.info("正在获取最新的稳定版本标签...")
                    
                    # 获取所有发布版本信息
                    repo_urls = [
                        "https://api.github.com/repos/KK-325/mediamaster-v2/releases/latest",
                        "https://gh.llkk.cc/https://api.github.com/repos/KK-325/mediamaster-v2/releases/latest"
                    ]
                    
                    latest_stable_release = None
                    for repo_url in repo_urls:
                        try:
                            response = requests.get(repo_url, timeout=8)
                            if response.status_code == 200:
                                latest_stable_release = response.json()
                                break
                        except Exception as e:
                            logger.warning(f"获取latest release失败: {e}")
                            continue
                    
                    if not latest_stable_release:
                        logger.error("无法获取最新的稳定版本信息")
                        last_error = "无法获取最新的稳定版本信息"
                        continue
                        
                    stable_version_tag = latest_stable_release.get("tag_name")
                    logger.info(f"最新的稳定版本标签: {stable_version_tag}")
                    
                    # 拉取指定标签的代码
                    logger.info("正在从 Git 仓库拉取最新稳定版本代码...")
                    try:
                        fetch_result = subprocess.run(
                            ['git', 'fetch', '--all'],
                            capture_output=True,
                            text=True,
                            cwd='/app',
                            timeout=60
                        )
                    except subprocess.TimeoutExpired:
                        error_message = "Git fetch 超时（60秒）"
                        logger.error(error_message)
                        last_error = error_message
                        continue
                    
                    if fetch_result.returncode != 0:
                        error_message = f"Git fetch 失败: {fetch_result.stderr}"
                        logger.error(error_message)
                        last_error = fetch_result.stderr
                        continue
                    
                    # 检出特定标签
                    logger.info(f"正在检出稳定版本 {stable_version_tag}...")
                    try:
                        checkout_result = subprocess.run(
                            ['git', 'checkout', stable_version_tag],
                            capture_output=True,
                            text=True,
                            cwd='/app',
                            timeout=30
                        )
                    except subprocess.TimeoutExpired:
                        error_message = "Git checkout 超时（30秒）"
                        logger.error(error_message)
                        last_error = error_message
                        continue
                    
                    if checkout_result.returncode != 0:
                        error_message = f"Git checkout 失败: {checkout_result.stderr}"
                        logger.error(error_message)
                        last_error = checkout_result.stderr
                        continue
                    
                    # 拉取代码
                    try:
                        pull_result = subprocess.run(
                            ['git', 'pull', 'origin', stable_version_tag],
                            capture_output=True,
                            text=True,
                            cwd='/app',
                            timeout=60
                        )
                    except subprocess.TimeoutExpired:
                        last_error = "Git pull 超时（60秒）"
                        logger.warning(f"Git 拉取稳定版本失败: {last_error}")
                        continue
                    
                    if pull_result.returncode == 0:
                        logger.info(f"Git 拉取稳定版本成功: {pull_result.stdout}")
                        git_pull_success = True
                        break
                    else:
                        last_error = pull_result.stderr
                        logger.warning(f"Git 拉取稳定版本失败: {last_error}")
                        
            except Exception as e:
                last_error = str(e)
                logger.warning(f"使用地址 {proxy_url} 更新失败: {e}")
                continue
        
        if not git_pull_success:
            # 备用方案：通过下载 release tar 包的方式更新
            logger.info("所有 git 代理均失败，尝试通过下载 tar 包的方式更新...")
            tarball_success = False
            try:
                import tempfile, tarfile, shutil
                # 重新获取目标版本标签
                target_tag = None
                if update_type == 'prerelease':
                    for repo_url in [
                        "https://api.github.com/repos/KK-325/mediamaster-v2/releases",
                        "https://gh.llkk.cc/https://api.github.com/repos/KK-325/mediamaster-v2/releases"
                    ]:
                        try:
                            response = requests.get(repo_url, timeout=8)
                            if response.status_code == 200:
                                for release in response.json():
                                    if release.get('prerelease'):
                                        target_tag = release.get('tag_name')
                                        break
                                if target_tag:
                                    break
                        except Exception:
                            continue
                else:
                    for repo_url in [
                        "https://api.github.com/repos/KK-325/mediamaster-v2/releases/latest",
                        "https://gh.llkk.cc/https://api.github.com/repos/KK-325/mediamaster-v2/releases/latest"
                    ]:
                        try:
                            response = requests.get(repo_url, timeout=8)
                            if response.status_code == 200:
                                target_tag = response.json().get('tag_name')
                                break
                        except Exception:
                            continue

                if target_tag:
                    logger.info(f"目标版本标签: {target_tag}")
                    tarball_urls = [
                        f"https://github.com/KK-325/mediamaster-v2/archive/refs/tags/{target_tag}.tar.gz",
                        f"https://gh.llkk.cc/https://github.com/KK-325/mediamaster-v2/archive/refs/tags/{target_tag}.tar.gz",
                    ]
                    for tarball_url in tarball_urls:
                        try:
                            logger.info(f"尝试下载 tar 包: {tarball_url}")
                            response = requests.get(tarball_url, timeout=120, stream=True)
                            if response.status_code == 200:
                                with tempfile.NamedTemporaryFile(suffix='.tar.gz', delete=False) as tmp_file:
                                    for chunk in response.iter_content(chunk_size=8192):
                                        tmp_file.write(chunk)
                                    tmp_path = tmp_file.name

                                extract_dir = tempfile.mkdtemp()
                                with tarfile.open(tmp_path, 'r:gz') as tar:
                                    tar.extractall(extract_dir)

                                extracted_items = os.listdir(extract_dir)
                                if extracted_items:
                                    source_dir = os.path.join(extract_dir, extracted_items[0])
                                    for item in os.listdir(source_dir):
                                        if item == '.git':
                                            continue
                                        src = os.path.join(source_dir, item)
                                        dst = os.path.join('/app', item)
                                        if os.path.isdir(src):
                                            if os.path.exists(dst):
                                                shutil.rmtree(dst)
                                            shutil.copytree(src, dst)
                                        else:
                                            shutil.copy2(src, dst)
                                    tarball_success = True
                                    logger.info("通过下载 tar 包方式更新成功")

                                os.unlink(tmp_path)
                                shutil.rmtree(extract_dir)
                                break
                        except Exception as e:
                            logger.warning(f"下载 tar 包失败 ({tarball_url}): {e}")
                            continue
            except Exception as e:
                logger.error(f"tar 包下载备用方案失败: {e}")

            if not tarball_success:
                error_message = f"所有地址更新均失败，最后错误信息: {last_error}"
                logger.error(error_message)
                return jsonify({"error": error_message}), 500

        # 步骤2.5: 重新删除定制版本废弃的解析器文件和模板文件
        # git checkout . / git pull 可能恢复这些文件，需在更新后重新清理
        obsolete_files = [
            '/app/movie_bthd.py',
            '/app/tvshow_hdtv.py',
            '/app/movie_tvshow_btsj6.py',
            '/app/movie_tvshow_seedhub.py',
            '/app/templates/recommendations.html',
            '/app/templates/manual_search.html'
        ]
        for obsolete_file in obsolete_files:
            if os.path.exists(obsolete_file):
                os.remove(obsolete_file)
                logger.info(f"已删除废弃文件: {obsolete_file}")

        # 步骤3: 安装依赖（如果有新的依赖）
        logger.info("正在安装新依赖...")
        install_result = subprocess.run(
            ['pip', 'install', '-r', 'requirements.txt', '--index-url', 'https://mirrors.aliyun.com/pypi/simple/'],
            capture_output=True,
            text=True,
            cwd='/app'
        )

        if install_result.returncode != 0:
            error_message = f"依赖安装失败: {install_result.stderr}"
            logger.error(error_message)
            return jsonify({"error": error_message}), 500

        logger.info(f"依赖安装成功: {install_result.stdout}")

        # 步骤4: 返回成功消息
        logger.info("执行更新已完成！")
        response = jsonify({
            "message": "更新成功！系统将结束主进程并自动重启。如未自动重启，请手动重启容器。",
            "current_version": current_version
        }), 200
        
        # 异步重启容器
        def restart_container():
            logger.info("正在重启容器...")
            time.sleep(2)
            # 查找并结束主进程
            target_process_name = "main.py"
            found_process = False
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    # 检查进程是否运行了 main.py
                    if target_process_name in (proc.info['cmdline'] or []):
                        logger.info(f"找到目标进程: PID={proc.info['pid']}, CMD={proc.info['cmdline']}")
                        proc.terminate()  # 发送终止信号
                        proc.wait(timeout=5)  # 等待进程结束
                        found_process = True
                        logger.info(f"已成功结束进程: PID={proc.info['pid']}")
                except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                    continue

            if not found_process:
                logger.warning("未找到运行中的 python main.py 进程")
        
        # 启动后台线程执行重启操作
        threading.Thread(target=restart_container, daemon=True).start()
        
        return response
        
    except Exception as e:
        logger.error(f"执行更新失败: {e}")
        return jsonify({"error": "更新过程中发生未知错误，请查看日志了解详情。"}), 500

# ===================== 产品文档路由 =====================
import urllib.parse

DOCS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'docs')
DOCS_SUBDIR = '使用帮助及常见问题'


def _scan_docs_tree():
    """扫描 docs 目录结构，返回嵌套的目录树 JSON。"""
    def scan_dir(dir_path, rel_prefix=''):
        items = []
        try:
            entries = sorted(os.listdir(dir_path))
        except OSError:
            return items
        # 先文件，后文件夹
        subdirs = []
        files = []
        for name in entries:
            full = os.path.join(dir_path, name)
            if os.path.isdir(full):
                subdirs.append(name)
            elif name.lower().endswith('.md'):
                files.append(name)
        for name in files:
            child_rel = f"{rel_prefix}{name}" if rel_prefix else name
            title = name[:-3]  # 去掉 .md
            items.append({
                'type': 'file',
                'name': title,
                'path': child_rel,
            })
        for name in subdirs:
            full = os.path.join(dir_path, name)
            child_rel = f"{rel_prefix}{name}" if rel_prefix else name
            children = scan_dir(full, f"{child_rel}/")
            items.append({
                'type': 'dir',
                'name': name,
                'path': child_rel,
                'children': children
            })
        return items

    if not os.path.isdir(DOCS_DIR):
        return []
    return scan_dir(DOCS_DIR)


@app.route('/docs')
def docs_page():
    return render_template('docs.html')


@app.route('/docs/api/list')
def docs_api_list():
    tree = _scan_docs_tree()
    from flask import jsonify as _jsonify
    return _jsonify({'tree': tree})


@app.route('/docs/api/content/<path:doc_path>')
def docs_api_content(doc_path):
    doc_path = urllib.parse.unquote(doc_path)
    # 防目录穿越
    if '..' in doc_path or doc_path.startswith('/') or doc_path.startswith('\\'):
        abort(404)
    full_path = os.path.normpath(os.path.join(DOCS_DIR, doc_path))
    if not full_path.startswith(os.path.normpath(DOCS_DIR)):
        abort(404)
    if not os.path.isfile(full_path) or not full_path.lower().endswith('.md'):
        abort(404)
    try:
        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception:
        abort(404)
    from flask import jsonify as _jsonify
    return _jsonify({'content': content, 'title': os.path.splitext(os.path.basename(doc_path))[0]})


if __name__ == '__main__':
    logger.info("程序已启动")

    # Ensure DB schema exists for local/dev runs
    try:
        import database_manager

        # Keep database_manager using the same DB path
        os.environ['DB_PATH'] = DATABASE
        os.environ['DATABASE'] = DATABASE
        database_manager.initialize_database()
    except Exception as e:
        logger.warning(f"数据库初始化跳过/失败（可能影响登录/设置功能）: {e}")

    # 创建硬链接
    src_dir = '/config/avatars'
    dst_dir = '/app/static/uploads/avatars'
    create_soft_link(src_dir, dst_dir)
    
    # 支持通过环境变量设置端口，默认为8888
    port = 8888
    try:
        port_env = os.environ.get('PORT')
        if port_env:
            port = int(port_env)
            logger.info(f"使用自定义端口: {port}")
        else:
            logger.info("使用默认端口: 8888")
    except (ValueError, TypeError):
        logger.warning(f"环境变量PORT值无效，使用默认端口: 8888")
    
    app.run(host='0.0.0.0', port=port, debug=False)