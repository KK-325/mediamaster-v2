## 说明
- 媒体库元数据刮削主要用于Emby、Jellyfin等媒体服务器，开启后程序会对媒体库所有媒体进行刮削并保存NFO元数据文件、海报（poster.jpg）、背景（fanart.jpg）等文件。（飞牛、绿联、极空间等自带刮削功能比较好用的媒体服务器可以不启用本功能）
- 元数据刮削功能使用TMDB API接口，如本地网络无法与TMDB连通可能导致刮削失败。
- 如因网络问题或DNS受到了污染无法正确解析到TMDB的IP，可通过修改Hosts文件方式解决。可参考：[CheckTMDB](https://github.com/cnwikee/CheckTMDB "CheckTMDB") 
【该项目提供：每日自动更新TMDB，themoviedb、thetvdb 国内可正常连接IP，解决DNS污染，供tinyMediaManager(TMM削刮器)、Kodi的刮削器、群晖VideoStation的海报墙、Plex Server的元数据代理、Emby Server元数据下载器、Infuse、Nplayer等正常削刮影片信息。】

### 元数据刮削功能使用介绍

#### 1、在设置中开启“媒体元数据刮削”
![刮削1](http://wiki.songmy.top:8080/server/index.php?s=/api/attachment/visitFile&sign=40562055a299c549b1f409c38ce34452 "刮削1")

#### 2、通过服务控制面板运行或查看运行日志
![刮削2](http://wiki.songmy.top:8080/server/index.php?s=/api/attachment/visitFile&sign=e21a2a2a93d4193a616fbb2286db6a7e "刮削2")

![刮削3](http://wiki.songmy.top:8080/server/index.php?s=/api/attachment/visitFile&sign=950216f2c26e37ed895fc183ec17da69 "刮削3")

#### 4、刮削成功后在媒体库目录中将成功保存元数据文件
![刮削4](http://wiki.songmy.top:8080/server/index.php?s=/api/attachment/visitFile&sign=c3587aa6111237f8ebb992f3923390dc "刮削4")

![刮削5](http://wiki.songmy.top:8080/server/index.php?s=/api/attachment/visitFile&sign=f99ab290007396cff55383fe7871a536 "刮削5")

#### 5、媒体服务器中成功读取媒体元数据
**未刮削元数据效果图：**

![刮削6](http://wiki.songmy.top:8080/server/index.php?s=/api/attachment/visitFile&sign=c42fd80292bd1933368cd1077eba0128 "刮削6")

**完成刮削元数据效果图：**

![刮削9](http://wiki.songmy.top:8080/server/index.php?s=/api/attachment/visitFile&sign=2acbb2d8a72da82b7defcb41fc66ac0f "刮削9")

![刮削7](http://wiki.songmy.top:8080/server/index.php?s=/api/attachment/visitFile&sign=1047bde8979c0b0641163626d29d1e9e "刮削7")

![刮削8](http://wiki.songmy.top:8080/server/index.php?s=/api/attachment/visitFile&sign=19d794e2430580f2024a928ac1e34a64 "刮削8")