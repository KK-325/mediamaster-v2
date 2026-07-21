## 说明

<div style="color: red;">
必须获取自己的豆瓣账号ID，并填写到豆瓣订阅用户ID中。程序通过获取豆瓣想看数据进行订阅、资源搜索等。
</div>

### 方法1：通过网页版获取

访问网页版豆瓣：[豆瓣](https://www.douban.com/mine/ "豆瓣")

登录豆瓣账号后，进入“个人主页”或“我的豆瓣”在页面中获取豆瓣ID

![设置3](http://wiki.songmy.top:8080/server/index.php?s=/api/attachment/visitFile&sign=cc3872715fc4826ef58124c725013160 "设置3")

### 方法2：通过豆瓣APP获取

打开手机APP，点击头像查看账号信息，复制“豆瓣ID”即可

![豆瓣APP1](http://wiki.songmy.top:8080/server/index.php?s=/api/attachment/visitFile&sign=5993af7e79679395525d0417027601da "豆瓣APP1")

### 系统设置中替换豆瓣ID

在系统设置中将默认的your_douban_id更改为自己的豆瓣ID，多个豆瓣ID可以用英文逗号分隔：

![豆瓣ID](http://wiki.songmy.top:8080/server/index.php?s=/api/attachment/visitFile&sign=b22887e6ada8ad04dadb57228f629ec7 "豆瓣ID")

使用以下链接并将链接中的your_douban_id更改为自己的豆瓣ID进行验证：

`https://www.douban.com/feed/people/your_douban_id/interests`

在浏览器中打开是能看到类似下图中订阅内容，则表示可以获取到豆瓣想看数据：

![豆瓣想看数据](http://wiki.songmy.top:8080/server/index.php?s=/api/attachment/visitFile&sign=3194ed360a5d21e6baad2657b19fdacd "豆瓣想看数据")

### 通过豆瓣进行影片订阅

通过豆瓣APP添加自己想看的影片：

方式1：通过海报左上角按钮添加

![豆瓣订阅1](http://wiki.songmy.top:8080/server/index.php?s=/api/attachment/visitFile&sign=d281d38d74d8facc7c2c5f125c8e2925 "豆瓣订阅1")

方式2：通过简介页面中的按钮添加

![豆瓣订阅2](http://wiki.songmy.top:8080/server/index.php?s=/api/attachment/visitFile&sign=c2c3d9f5e3c337b291225a1304d5a166 "豆瓣订阅2")

所有配置结束后，在“服务控制”中运行“获取豆瓣想看”任务，即可成功获取到豆瓣想看的影片信息

![获取豆瓣数据](http://wiki.songmy.top:8080/server/index.php?s=/api/attachment/visitFile&sign=85701fdec0eebff366b31dc5a1231c02 "获取豆瓣数据")

![获取豆瓣数据-日志](http://wiki.songmy.top:8080/server/index.php?s=/api/attachment/visitFile&sign=200dc47994fad156170df689c926b546 "获取豆瓣数据-日志")

![豆瓣想看](http://wiki.songmy.top:8080/server/index.php?s=/api/attachment/visitFile&sign=c09e6d0b60c6a414d1fe8bd68e2db531 "豆瓣想看")