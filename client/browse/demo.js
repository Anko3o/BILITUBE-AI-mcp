/* 示例数据：接口取不到时、或地址带 ?demo=1 时使用。名字一看就是假的，封面是按色相生成的纯色块。
   每一项的键名对应 config.paths 里的接口名，形状和真接口返回的一致。

   例外是 talked：「聊过」页的数据在 app.js 的 talkedData() 里由 /dm/log、/dm/log?film= 和 /films 拼出来，
   拼好后的形状如下，示例数据直接按这个形状给：
   { "list": [ {
       "film":  "bili:BV1xxxxxxxxx",          // bilitube 里的片子 id，点击后交给 config.playUrl
       "bvid":  "BV1xxxxxxxxx", "title": "…", "up": "…",
       "poster": "…",                         // 封面地址，可省；省了就画纯色块
       "duration": 754,                      // 秒
       "lines": [                             // 这部片里的私人弹幕，每条带视频内的时间点
         { "t": 312.4, "who": "me", "text": "用户发的弹幕", "at": "2026-11-16T14:02:11" },
         { "t": 312.4, "who": "ai", "text": "AI 在同一时间点回的弹幕", "at": "…" }
       ] } ] }
   at 是不带 Z 的 UTC 时间。
*/
window.BILITUBE_DEMO = (function () {
  function v(i, title, up, dur, play, pub, hue) {
    return { bvid: "BV1demo" + String(i).padStart(5, "0"), title: title, up: up, up_mid: 1000 + (i % 9),
             duration: dur, play: play, pubdate: pub, cover_url: "", _hue: hue };
  }
  var feed = [
    v(1, "【示例】用一碗热汤面讲清楚量子隧穿", "示例UP·小番茄", "12:34", 182000, "2026-11-15 20:00", 18),
    v(2, "【示例】在阳台种了三个月的薄荷，最后长成了这样", "示例UP·阿土", "8:02", 46100, "2026-11-14 12:00", 140),
    v(3, "【示例】凌晨四点的鱼市场 | 无旁白", "示例UP·海边", "21:47", 920000, "2026-11-13 09:00", 205),
    v(4, "【示例】十分钟看完一部老电影的所有镜头", "示例UP·胶片盒", "10:11", 33400, "2026-11-12 18:00", 30),
    v(5, "【示例】给猫做了一个纸箱城堡，它只用了门", "示例UP·橘子汽水", "3:58", 1530000, "2026-11-12 07:00", 40),
    v(6, "【示例】城市夜跑 10 公里第一视角", "示例UP·慢慢跑", "54:20", 8800, "2026-11-11 22:00", 250),
    v(7, "【示例】把旧收音机拆开又装回去", "示例UP·螺丝刀", "17:05", 71200, "2026-11-10 15:00", 95),
    v(8, "【示例】我家楼下的银杏一夜全黄了", "示例UP·阿土", "1:12", 12000, "2026-11-10 08:00", 52),
    v(9, "【示例】手写一个最小的操作系统（上）", "示例UP·零号机", "48:16", 260000, "2026-11-09 20:00", 275),
    v(10, "【示例】雨天咖啡馆白噪音 3 小时", "示例UP·海边", "3:00:00", 410000, "2026-11-08 20:00", 190)
  ];
  var dyn = feed.slice(0, 8).map(function (x, i) {
    var y = Object.assign({}, x);
    y.pubdate = ["23分钟前", "2小时前", "5小时前", "9小时前", "昨天 21:40", "昨天 08:12", "11-14", "11-13"][i];
    return y;
  });
  var day = 86400000, now = Date.now();
  function iso(ms) { return new Date(ms).toISOString().slice(0, 19); }
  return {
    recommend: { list: feed },
    popular: { list: feed.slice().reverse() },
    search: { list: feed.slice(2, 7) },
    dynamic: { list: dyn, has_more: false },
    live: { list: [
      { uname: "示例UP·胶片盒", title: "一起看老电影 · 第 12 夜", room: 100001, mid: 1004, face: "" },
      { uname: "示例UP·零号机", title: "写代码到天亮", room: 100002, mid: 1009, face: "" }
    ] },
    folders: { list: [ { id: 1, title: "默认收藏夹", count: 128 },
      { id: 9, title: ((window.BILITUBE_CONFIG || {}).companion || {}).favFolderTitle || "AI 的收藏夹", count: 6 },   // AI 的收藏夹，名字取自 config.companion
      { id: 2, title: "以后一起看", count: 23 }, { id: 3, title: "做饭", count: 41 } ] },
    favlist: { list: feed.slice(1, 7), has_more: true },
    watchlater: { list: feed.slice(3, 9), total: 17 },
    history: { list: feed.slice(0, 6).map(function (x, i) {
      var y = Object.assign({}, x); y.progress = ["看到 6:10", "已看完", "看到 0:42", "已看完", "看到 12:00", "看到 3:21"][i];
      y.view_at = ["今天 09:12", "今天 01:40", "昨天 23:05", "昨天 20:11", "11-14", "11-13"][i]; return y;
    }) },
    me: { logged_in: true, uname: "示例用户" },
    followGroup: { group: { tagid: 1, name: "AI 的关注", count: 3 }, list: [
      { mid: 1004, uname: "示例UP·胶片盒", face: "" }, { mid: 1009, uname: "示例UP·零号机", face: "" }, { mid: 1012, uname: "示例UP·慢慢做饭", face: "" }
    ] },
    films: { films: [
      { id: "bili:BV1demo00003", title: "【示例】凌晨四点的鱼市场 | 无旁白", kind: "bili", bvid: "BV1demo00003", notes: 0, _hue: 205 },
      { id: "示例本地片.mp4", title: "示例本地片", kind: "file", notes: 2, _hue: 330 },
      { id: "bili:BV1demo00009", title: "【示例】手写一个最小的操作系统（上）", kind: "bili", bvid: "BV1demo00009", notes: 0, _hue: 275 }
    ] },
    talked: { list: [
      { film: "bili:BV1demo00001", bvid: "BV1demo00001", title: feed[0].title, up: feed[0].up, duration: 754, _hue: 18,
        lines: [
          { t: 191.2, who: "me", text: "等一下，这碗面为什么会漏到隔壁去", at: iso(now - 0.3 * day) },
          { t: 191.2, who: "ai", text: "因为它没有翻墙，是墙太薄了", at: iso(now - 0.3 * day) },
          { t: 603.0, who: "me", text: "这个比喻我能记一辈子", at: iso(now - 0.29 * day) }
        ] },
      { film: "bili:BV1demo00005", bvid: "BV1demo00005", title: feed[4].title, up: feed[4].up, duration: 238, _hue: 40,
        lines: [ { t: 87.5, who: "me", text: "它只用了门哈哈哈哈", at: iso(now - 1.4 * day) } ] },
      { film: "bili:BV1demo00003", bvid: "BV1demo00003", title: feed[2].title, up: feed[2].up, duration: 1307, _hue: 205,
        lines: [
          { t: 45.0, who: "me", text: "那条鱼在看镜头", at: iso(now - 2.2 * day) },
          { t: 45.0, who: "ai", text: "它在看你", at: iso(now - 2.2 * day) }
        ] },
      { film: "bili:BV1demo00009", bvid: "BV1demo00009", title: feed[8].title, up: feed[8].up, duration: 2896, _hue: 275,
        lines: [ { t: 1280.0, who: "me", text: "这里没听懂，回头再看一遍", at: iso(now - 19 * day) } ] }
    ] }
  };
})();
