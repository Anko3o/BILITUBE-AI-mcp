/* bilitube · 用户和 AI 的显示名字、AI 的签名、两人的头像、自定义皮肤的底图。
   存在服务器上（做法参考 eryu 的 /music/profile），换浏览器、换设备都还在；本地 localStorage 只留一份缓存，开页先用。
   别的页（比如播放页）也能用：先加载 config.js（可省），再加载这个文件——
     <script src="browse/prefs.js"></script>
     BilitubePrefs.load().then(function (p) { p.companionName; p.userName; p.avatars.companion; });
     BilitubePrefs.companionName()          // 同步取，用上次缓存的值，没有就用 config 里的默认
     window.addEventListener("bilitube:prefs", function (e) { e.detail … })   // 每次存好或拉到新值时广播
   接口（都在 {api} 下，鉴权在反向代理那层）：
     GET  {api}/prefs → {ok, companionName, userName, companionBio, avatars:{user, companion}, skinBg}
          没改过的名字、没传过的图都是 null。load() 返回的是补好默认值的那份（companionName 一定有值；
          userName 可能仍是 null，这时页面改用 /bili/me 里的 B 站昵称）。
     POST {api}/prefs {companionName?, userName?, companionBio?}   传空串＝恢复默认
     POST {api}/avatar/user | {api}/avatar/companion | {api}/skin/bg   请求体是图片原始字节（头像 ≤240KB，底图 ≤250KB），空请求体＝删除 */
window.BilitubePrefs = (function () {
  var C = window.BILITUBE_CONFIG || {}, API = C.api || "../api", KEY = (C.storePrefix || "bilitube_") + "prefs";
  var cur = null;
  try { cur = JSON.parse(localStorage.getItem(KEY) || "null"); } catch (e) {}
  function fill(p) {
    p = p || {};
    var cp = C.companion || {};
    return {
      companionName: p.companionName || cp.name || "AI",
      userName: p.userName || null,
      companionBio: p.companionBio || cp.bio || "",
      avatars: p.avatars || { user: null, companion: null },
      skinBg: p.skinBg || null,
      raw: { companionName: p.companionName || null, userName: p.userName || null, companionBio: p.companionBio || null }
    };
  }
  function keep(p) {
    cur = p;
    try { localStorage.setItem(KEY, JSON.stringify(p)); } catch (e) {}
    try { window.dispatchEvent(new CustomEvent("bilitube:prefs", { detail: fill(p) })); } catch (e) {}
    return fill(p);
  }
  function load() {
    return fetch(API + "/prefs", { credentials: "same-origin", cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(keep)
      .catch(function () { return fill(cur); });
  }
  function save(patch) {
    return fetch(API + "/prefs", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch || {}) })
      .then(function (r) { return r.json().then(function (d) { if (!r.ok || d.error) throw new Error(d.error || "HTTP " + r.status); return keep(d); }); });
  }
  // which: "user" | "companion" | "skinbg"；blob 为 null＝删除这张图
  function upload(which, blob) {
    var path = which === "skinbg" ? "/skin/bg" : "/avatar/" + (which === "companion" ? "companion" : "user");
    return fetch(API + path, { method: "POST", credentials: "same-origin", headers: { "Content-Type": (blob && blob.type) || "image/jpeg" }, body: blob || "" })
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) { if (!r.ok || d.error) throw new Error(d.error || "HTTP " + r.status); return keep(d.prefs || cur); }); });
  }
  return {
    load: load, save: save, upload: upload,
    cached: function () { return fill(cur); },
    companionName: function () { return fill(cur).companionName; }
  };
})();
