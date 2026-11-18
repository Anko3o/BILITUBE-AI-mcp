/* bilitube 逛片页的配置。部署时要改的都在这个文件里，其他文件不用动。
   · 账号名、头像、关注列表都不写在这里，页面运行时从后端接口取。
   · 所有地址都相对本页（<前缀>/browse/）书写，整个站挂在哪个路径前缀下都不用改。
   · 一般只需要看三处：api（后端在哪）、homeUrl（左上角要不要「回主页」）、companion（一起看的 AI 叫什么、用哪个收藏夹）。 */
window.BILITUBE_CONFIG = {
  /* 后端根地址。默认部署里后端经反向代理挂在 <前缀>/api（见 examples/Caddyfile），所以写 "../api"。
     后端放在别处就改成那个地址（同源，页面靠同源 cookie 过门禁）。 */
  api: "../api",

  /* 各接口相对 api 的路径。值里的 {名字} 由 app.js 填入并做 URL 编码。
     后端用的是仓库自带的 server/bilitube_server.py 就不用改；自己换了后端，才按这里的形状对接。 */
  paths: {
    /* ── 只读（GET） ── */
    recommend: "/bili/recommend?n=20&fresh={fresh}",        // 推荐；fresh 每次加一＝换一批
    popular:   "/bili/popular?n=20&page={page}",            // 热门
    search:    "/bili/search?n=20&page={page}&q={q}",       // 搜视频
    dynamic:   "/bili/dynamic?n=30&page={page}",            // 关注的人的新投稿；app.js 会追加 &host_mid= 只看一个人
    live:      "/bili/live?n=20",                           // 关注的人里正在直播的
    folders:   "/bili/folders",                             // 自己的收藏夹列表
    favlist:   "/bili/favlist?n=20&page={page}&media_id={id}", // 一个收藏夹里的视频
    favpeek:   "/bili/favlist?n=3&page=1&media_id={id}",    // 同上，只取前三条做预览
    watchlater:"/bili/watchlater?n=20&offset={offset}",     // 稍后再看
    history:   "/bili/history?n=20",                        // 历史记录；翻页游标由 app.js 追加
    pgcsearch: "/bili/pgc/search?q={q}",                    // 搜索结果顶上的番剧 / 影视（只在第一页请求）
    pgcfollow: "/bili/pgc/follow?type={type}&page={page}",  // 追番（type=1）/ 追剧（type=2）
    me:        "/bili/me",                                  // 当前 B 站登录状态 {logged_in, uname, face, mid}
    films:     "/films",                                    // 后端认得的片子（本地片＋开过的 B 站视频的底账；页面只拿它列本地片、补封面）
    notes:     "/notes?film={film}",                        // 一部片时间轴上的批注（本页暂未用到）
    talkedList:"/dm/log",                                   // 有私人弹幕记录的片 {films:[{film,title,user,companion,first,last}]}
    talkedFilm:"/dm/log?film={film}",                       // 一部片的私人弹幕 {film,title,count,items:[{t,author:"user"|"companion",text,at}]}，at 是不带 Z 的 UTC
    poster:    "/bili/cover?id={film}",                     // 后端认得的一部片的封面（本页暂未用到：/films 自带 poster 地址）
    pages:     "/bili/pages?id={bvid}",                     // 一个 B 站视频的标题和分 P（后端已不认得的片靠它补片名）
    cover:     "/bili/listcover?bvid={bvid}&u={u}",         // 视频封面的后端代理（coverMode=proxy 时用，direct 时直连没拿到才退回它）
    face:      "/bili/face?mid={mid}&u={u}",                // 用户头像，同样经后端代理
    followGroup:"/bili/follow_group?n=30&page={page}",      // AI 自己关注的 UP（专属关注分组）{group|null, want:{id,name}, list:[{mid,uname,face}]}
    /* ── 会改数据的（POST），只在用户亲手操作时才发 ── */
    remove:    "/remove",                                   // {id}：删一部本地片（连文件一起删）
    laterDel:  "/bili/watchlater",                          // {id, remove:true}：从 B 站「稍后再看」里移除
    loginQr:   "/bili/login/qr",                            // 生成 B 站扫码登录的二维码
    loginState:"/bili/login/state",                         // （GET）扫码进度 {progress: waiting|ok|expired|timeout|error, logged_in, uname}
    logout:    "/bili/logout"                               // 退出 B 站登录（设置 → 账号 → 退出 → 确认）→ {ok, remote: ok|unconfirmed|skipped, note}
  },

  /* 点一部片后跳去的播放页地址。播放页认这些 film 值：
       BV 号 / bili:BV…（可带 _p2 指定分 P）/ 本地片 id；
       番剧影视 bili:ep…（指定一集）/ bili:ss…（整部，从上次看到的那一集开始）。
     &play=1 表示进页就播，&t= 表示从第几秒开始。 */
  playUrl: function (film, t) {
    return "../?film=" + encodeURIComponent(film) + "&play=1" + (t ? "&t=" + Math.floor(t) : "");
  },
  /* 直播：播放页认 ?film=live:<房间号>。 */
  liveUrl: function (room) {
    return "../?film=live:" + encodeURIComponent(room);
  },
  openLive: function (room) { location.href = this.liveUrl(room); },

  /* 左上角「回主页」按钮指向的地址，比如你自己的导航页；null＝不显示这颗按钮。 */
  homeUrl: null,

  /* 封面怎么拿：
     · "direct"（默认）：浏览器直接从 B 站图床要小图（webp，一张 5～7KB），不带 Referer；直连失败的那张自动退回后端代理。
       后端和看的人不在同一个地区时（比如服务器在海外、人在国内），这样快得多。
     · "proxy"：一律经后端 /bili/listcover 转（图床对你那边的网络不通时用）。
     单台设备上也能临时改：localStorage 里 <storePrefix>cover 设成 "proxy" / "direct"。 */
  coverMode: "direct",

  /* 一起看的那位 AI。设成 null 则整块不用（「我的」页不出现 AI 的入口卡和主页）。
     · name：AI 的默认名字。用户在设置里改过的名字存在服务器（/prefs），以那个为准。
     · author：私人弹幕记录（/dm/log）里 author 等于这个值的条目算 AI 发的。和后端保持一致，一般不用改。
     · favFolderTitle：AI 的收藏夹在 B 站上叫什么。它其实是建在用户自己 B 站账号里的一个收藏夹（建议设为私密），
       页面打开时按名字从用户的收藏夹里把它认出来，单独放在 AI 的主页上。
     · favFolderId：也可以直接填这个收藏夹的数字 id；填了就只按 id 认，忽略名字。两项都不填＝不显示 AI 的收藏夹。
     · bio：AI 主页名字下面那行小字的默认值（设置里「签名」可改）；留空则显示「和 <用户名> 一起看」。 */
  companion: {
    name: "AI",
    author: "companion",
    favFolderTitle: "AI 的收藏夹",   // 改成你在 B 站上给 AI 建的那个收藏夹的名字
    favFolderId: null,
    bio: ""
  },

  /* 本页（逛片页）在 localStorage 里所有键的前缀（皮肤、日夜、主题色、名字缓存等）。同域名下装了多份时改成不同的值。
     播放页 ../index.html 不读这个文件，它的键固定以 bilitube_ 开头（日夜、弹幕样式、直播画质），同域名的几份会共用；
     日夜开关两页共用一个键，所以只有这里保持 "bilitube_" 时，在逛片页点的日夜才会带到播放页。 */
  storePrefix: "bilitube_",

  /* 预留：设置里是否提供「自定义」皮肤（自选主题色、自传底图）。当前版本还没读这一项，「自定义」总是在。 */
  showSkins: true,

  /* 自定义皮肤的底图默认显示多少（0～1，设置里的「背景填充」可调）。 */
  bgShowDefault: 0.3,

  /* 用户没在设置里选过日夜时，想跟随同域名下另一个页面的日夜设置，就填那个页面存在 localStorage 里的键名
     （值须为 "dark" 或 "light"）；null＝跟随系统的深浅色。 */
  followThemeKey: null
};
