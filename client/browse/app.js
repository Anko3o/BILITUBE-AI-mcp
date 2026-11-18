/* bilitube 逛片页 · 四个主页签：推荐 / 关注 / 聊过 / 我的，「我的」下面挂几张二级页。
   取数都是 GET；只有用户亲手操作才会发写请求：稍后再看 / 本地片的横滑删除、设置里的名字 / 头像 / 底图、扫码登录和退出。
   点一部片跳到 config.playUrl 去播。所有地址都在 config.js；接口取不到时退回 demo.js 的示例数据，并在列表顶上标「示例」。
   推荐页顶上三个标签（推荐 / 热门 / 直播），各页都能下拉刷新；用户和 AI 的名字、头像存在服务器（prefs.js）；
   皮肤、日夜和 B 站登录在设置卡里。过渡动画照 eryu 的做法：列表逐行入场、切页淡入、设置卡弹出、按压回弹。 */
(function () {
  "use strict";
  var C = window.BILITUBE_CONFIG || {}, D = window.BILITUBE_DEMO || {};
  var P = C.storePrefix || "bilitube_";
  var Q = new URLSearchParams(location.search);
  var DEMO = Q.get("demo") === "1";
  var CP = C.companion || null;   // 一起看的 AI（config.companion），null＝不显示 AI 相关的入口
  var AI_KEY = (CP && CP.author) || "companion";
  var PF = window.BilitubePrefs || null;
  var PR = PF ? PF.cached() : { companionName: (CP && CP.name) || "AI", userName: null, companionBio: "", avatars: {}, raw: {} };
  var $ = function (id) { return document.getElementById(id); };
  var esc = function (s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  };
  var store = {
    get: function (k) { try { return localStorage.getItem(P + k); } catch (e) { return null; } },
    set: function (k, v) { try { if (v == null || v === "") localStorage.removeItem(P + k); else localStorage.setItem(P + k, v); } catch (e) {} }
  };

  // ── 取数 ──
  function apiUrl(name, vars, extra) {
    var p = (C.paths || {})[name] || "";
    p = p.replace(/\{(\w+)\}/g, function (_, k) { return encodeURIComponent(vars && vars[k] != null ? vars[k] : ""); });
    if (extra) Object.keys(extra).forEach(function (k) {
      if (extra[k] != null && extra[k] !== "") p += (p.indexOf("?") < 0 ? "?" : "&") + k + "=" + encodeURIComponent(extra[k]);
    });
    return (C.api || "") + p;
  }
  function get(name, vars, extra, opt) {
    opt = opt || {};
    var fake = function (err) { return { data: D[name] || {}, demo: true, err: err }; };
    if (DEMO) return Promise.resolve(fake());
    return fetch(apiUrl(name, vars, extra), { credentials: "same-origin", cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    }).then(function (d) {
      if (d && d.error) throw new Error(d.error);
      return { data: d };
    }).catch(function (e) { return opt.noFake ? { data: null, err: e } : fake(e); });
  }
  function post(name, body) {
    return fetch(apiUrl(name), { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}), keepalive: true })
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) { if (!r.ok || d.error) throw new Error(d.error || "HTTP " + r.status); return d; }); });
  }
  // 封面默认由浏览器直连 B 站图床、要小图（卡片 480×270、一行一条的 320×180，webp，一张 5～7KB），
  // 不经后端转（后端和看的人不在一个地区时，绕一道很慢）。图床看见外站 Referer 会 403，所以 <img referrerpolicy="no-referrer">
  // （index.html 头上也有 same-origin 那条 meta）。直连没拿到（onerror）就退回后端代理那张；
  // config.coverMode = "proxy" 或 localStorage <storePrefix>cover=proxy 整个改走代理。
  var COVER_DIRECT = (function () {
    var m = C.coverMode || "direct";
    try { var o = localStorage.getItem(P + "cover"); if (o === "proxy" || o === "direct") m = o; } catch (e) {}
    return m !== "proxy";
  })();
  var HDSLB = /^(?:https?:)?\/\/[a-z0-9.-]+\.hdslb\.com\//i;
  function proxyCover(it) {
    if (it.cover_url && it.bvid) return apiUrl("cover", { bvid: it.bvid, u: it.cover_url });
    if (it.cover_url && it.season_id) return apiUrl("cover", { bvid: "ss" + it.season_id, u: it.cover_url });   // 番剧整部的封面
    if (it.poster) return it.poster;
    return "";
  }
  function directCover(it, small) {
    var u = String(it.cover_url || "");
    if (!COVER_DIRECT || !HDSLB.test(u) || u.indexOf("@") >= 0) return "";
    return u.replace(/^(?:https?:)?\/\//i, "https://") + (small ? "@320w_180h_1c.webp" : "@480w_270h_1c.webp");
  }
  function coverOf(it) { return proxyCover(it); }
  function hueOf(it) {
    if (it._hue != null) return it._hue;
    var s = String(it.bvid || it.title || ""), h = 0;
    for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 360;
    return h;
  }
  // 封面：底下一层暗渐变，左边放 left（播放量、弹幕之类），右边放 right（时长、看到哪）
  // 图是 <img loading=lazy>：先出文字，滑到附近才要图；直连失败换代理（下面 error 监听），再失败就换成色块。
  function cvHTML(it, left, right, small) {
    var d = directCover(it, small), p = proxyCover(it), u = d || p;
    if (right === undefined) right = it.duration ? fmtDur(it.duration) : "";
    var ov = (left || right) ? '<div class="ov">' + (left || "") + (right ? '<span class="r">' + esc(right) + "</span>" : "") + "</div>" : "";
    var img = u ? '<img class="cvi" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" src="' + esc(u) + '"' +
      (d && p && p !== d ? ' data-fb="' + esc(p) + '"' : "") + ">" : "";
    return '<div class="cv' + (u ? "" : " ph") + '" style="--h:' + hueOf(it) + '">' + img + ov + "</div>";
  }
  document.addEventListener("error", function (e) {
    var el = e.target;
    if (!el || el.tagName !== "IMG" || !el.classList.contains("cvi")) return;
    var fb = el.getAttribute("data-fb");
    if (fb) { el.removeAttribute("data-fb"); el.src = fb; return; }
    var cv = el.parentNode; el.remove();           // 两条路都没拿到：换成色块
    if (cv && cv.closest && cv.closest(".tk-film")) { cv.remove(); return; }   // 「聊过」那张卡：封面位整个拿掉
    if (cv && cv.classList) cv.classList.add("ph");
  }, true);
  document.addEventListener("load", function (e) {
    var el = e.target;
    if (el && el.tagName === "IMG" && el.classList.contains("cvi")) el.classList.add("on");
  }, true);
  function fmtDur(d) {
    if (typeof d === "string") return d;
    d = Math.round(+d || 0);
    var h = Math.floor(d / 3600), m = Math.floor(d % 3600 / 60), s = d % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(s).padStart(2, "0");
  }
  function fmtT(t) {
    t = Math.max(0, Math.floor(+t || 0));
    var h = Math.floor(t / 3600), m = Math.floor(t % 3600 / 60), s = t % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : String(m).padStart(2, "0")) + ":" + String(s).padStart(2, "0");
  }
  function fmtPlay(n) {
    if (n == null || n === "") return "";
    if (typeof n === "string" && !/^\d+$/.test(n)) return n;
    n = +n;
    if (n >= 1e8) return (n / 1e8).toFixed(1).replace(/\.0$/, "") + "亿";
    if (n >= 1e4) return (n / 1e4).toFixed(1).replace(/\.0$/, "") + "万";
    return String(n);
  }
  function filmKey(it) { return it.film || it.id || it.bvid; }
  function play(it, t) { var k = filmKey(it); if (!k) return toast("这条不是视频"); embedGo(C.playUrl(k, t)); }
  // 嵌入模式（?embed=1）：点片时在同一个 iframe 里换成播放页（网址补上 embed=1）。走之前把当前页签记进
  // sessionStorage，播放页的返回按钮据此换回来。两边都用 location.replace，不留历史记录，手机返回键不会在 iframe 里一格格往回退。
  var EMBED = document.documentElement.getAttribute("data-embed") === "1";
  function embedGo(u) {
    if (!EMBED) { location.href = u; return; }
    try { sessionStorage.setItem("bilitube_embed_back", location.hash.slice(1)); } catch (e) {}
    location.replace(u + (u.indexOf("?") < 0 ? "?" : "&") + "embed=1");
  }
  if (EMBED && C.liveUrl) C.openLive = function (room) { embedGo(C.liveUrl(room)); };
  // 提示条（照 eryu 的 toast）；传 act＝{label, fn} 时右边多一颗按钮（比如「撤销」）
  function toast(msg, act, ms) {
    var el = $("toast");
    el.innerHTML = "<span>" + esc(msg) + "</span>" + (act ? '<button type="button" class="toast-act">' + esc(act.label) + "</button>" : "");
    if (act) el.querySelector(".toast-act").onclick = function () { el.hidden = true; act.fn(); };
    el.hidden = false; el.style.animation = "none"; void el.offsetWidth; el.style.animation = "";
    clearTimeout(toast._t); toast._t = setTimeout(function () { el.hidden = true; }, ms || 2200);
  }
  function demoTag(r) { return r && r.demo ? '<span class="demo" title="' + esc(r.err ? "接口没拿到：" + r.err.message : "示例数据") + '">示例</span>' : ""; }
  function bindPlay(root, items) {
    root.querySelectorAll("[data-i]").forEach(function (el) {
      if (el.dataset.bound) return;
      el.dataset.bound = "1";
      el.addEventListener("click", function () { if (el.dataset.swiped) return; play(items[+el.dataset.i]); });
    });
  }

  var I = (window.BILITUBE_ICONS || { ic: function () { return ""; } }).ic;
  if (D && D.favlist && !D.favpeek) D.favpeek = { list: D.favlist.list.slice(0, 3) };

  // 入场动画：第 i 个元素延后 i × --stagger，第 14 个以后一起进（配合 app.css 的 .stagger-in）
  function sg(i) { return ' style="--i:' + Math.min(+i || 0, 14) + '"'; }

  // 封面左下角：播放量、弹幕数
  function ovStats(it) {
    var p = fmtPlay(it.play), d = fmtPlay(it.danmaku);
    return (p ? "<span>" + I("play", 12, 2) + esc(p) + "</span>" : "") + (d ? "<span>" + I("dm", 12, 2) + esc(d) + "</span>" : "");
  }
  // 推荐卡：数字压在封面上，下面只有两行标题＋一行 UP 主。k＝这一批里的第几张（入场错开用）
  function cardHTML(it, i, k) {
    return '<a class="vc stagger-in" data-i="' + i + '" role="button" tabindex="0"' + sg(k == null ? i : k) + ">" + cvHTML(it, ovStats(it)) +
      '<div class="t">' + esc(it.title) + '</div><div class="m">' + esc(it.up || "") + "</div></a>";
  }
  // 一行一条：左边封面（右下角叠 right），右边标题＋最多两行灰字
  function rowHTML(it, i, lines, right) {
    return '<a class="row" data-i="' + i + '" role="button" tabindex="0">' + cvHTML(it, "", right, true) +
      '<div class="tx"><div class="t">' + esc(it.title) + "</div>" +
      lines.filter(Boolean).map(function (l, k) { return '<div class="m"' + (k ? ' style="margin-top:0"' : "") + ">" + l + "</div>"; }).join("") + "</div></a>";
  }
  // 能横滑删除的一行：底下一层删除按钮，上面盖着普通的一行
  function swRowHTML(it, i, lines, right) {
    return '<div class="sw stagger-in" data-k="' + i + '"' + sg(i) + '><div class="sw-act" aria-hidden="true"><span class="sw-ic">' + I("trash", 20, 1.8) + "</span></div>" + rowHTML(it, i, lines, right) + "</div>";
  }
  function skeleton(n) {
    var s = '<div class="grid2">';
    for (var i = 0; i < (n || 4); i++) s += '<div class="vc"><div class="sk" style="aspect-ratio:16/10;border-radius:0"></div><div class="t"></div><div class="m">　</div></div>';
    return s + "</div>";
  }
  // 不带 Z 的 UTC 时间串 → 本地时区的 YYYY-MM-DD 和 HH:MM
  function local(at) {
    var s = String(at || ""); if (!s) return { day: "", hm: "" };
    var d = /Z|[+-]\d\d:?\d\d$/.test(s) ? new Date(s) : new Date(s + "Z");
    if (isNaN(d)) return { day: s.slice(0, 10), hm: s.slice(11, 16) };
    var p = function (n) { return String(n).padStart(2, "0"); };
    return { day: d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()), hm: p(d.getHours()) + ":" + p(d.getMinutes()) };
  }
  function niceDay(day) {
    var m = String(day || "").match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (!m) return "";
    var d = new Date(+m[1], +m[2] - 1, +m[3]), t = new Date(); t.setHours(0, 0, 0, 0);
    var diff = Math.round((t - d) / 864e5);
    return diff === 0 ? "今天" : diff === 1 ? "昨天" : (+m[1] === t.getFullYear() ? "" : m[1] + "-") + m[2] + "-" + m[3];
  }
  function moreBtn(label) { return '<div class="more-row"><button class="tab icon" type="button" data-more aria-label="再来一些">' + I("down", 14, 1.8) + (label || "") + "</button></div>"; }

  // ══ 用户和 AI 的名字、头像（存服务器 {api}/prefs，见 prefs.js） ══
  // 名字写进页面时一律包一层 data-nm="ai"（AI）/ "user"（用户），头像带 data-av="companion" / "user"；
  // 设置里一改，paintPeople() 把整页的名字和头像一起换掉，不重新取数。
  var ME = null, meP = null;
  function loadMe() { if (!meP) meP = get("me", null, null, { noFake: !DEMO }).then(function (r) { ME = r.data || {}; ME._demo = !!r.demo; ME._err = !!r.err && !r.demo; return r; }); return meP; }
  function aiName() { return PR.companionName || (CP && CP.name) || "AI"; }
  function userName() { return PR.userName || (ME && ME.logged_in && ME.uname) || "你"; }
  function aiBio() { return PR.companionBio || ""; }
  function avUrl(who) {
    var a = PR.avatars || {};
    if (who === "companion") return a.companion || "";
    if (a.user) return a.user;
    return ME && ME.logged_in && ME.face && ME.mid ? apiUrl("face", { mid: ME.mid, u: ME.face }) : "";
  }
  function nm(who) { return '<span data-nm="' + who + '">' + esc(who === "ai" ? aiName() : userName()) + "</span>"; }
  // 头像圆：有图贴图，没图是纯色圆里一个线条小人（不用首字母代替）
  function avHTML(who, cls, s, tag) {
    tag = tag || "span";
    return "<" + tag + ' class="av ' + (cls || "") + '" data-av="' + who + '" data-s="' + (s || 24) + '"' + (tag === "button" ? ' type="button" aria-label="换头像"' : "") + "></" + tag + ">";
  }
  function paintPeople(root) {
    (root || document).querySelectorAll("[data-nm]").forEach(function (el) { el.textContent = el.dataset.nm === "ai" ? aiName() : userName(); });
    (root || document).querySelectorAll("[data-av]").forEach(function (el) {
      var u = avUrl(el.dataset.av);
      if (u) { el.style.backgroundImage = "url('" + u.replace(/'/g, "%27") + "')"; el.classList.remove("ph"); el.innerHTML = ""; }
      else { el.style.backgroundImage = ""; el.classList.add("ph"); el.innerHTML = I("user", +el.dataset.s || 24, 1.5); }
    });
    var bio = $("palSub");
    if (bio) bio.innerHTML = aiBio() ? esc(aiBio()) : "和 " + nm("user") + " 一起看";
    if (curPage === "companion") $("topTitle").textContent = aiName() + "的主页";
  }
  function onPrefs(p) { if (p) PR = p; paintPeople(); }
  if (PF) window.addEventListener("bilitube:prefs", function (e) { onPrefs(e.detail); });
  // 选图 → 在浏览器里缩成 jpg（头像长边 300、质量 0.85，参数照 eryu 的 pickImage）→ 传服务器
  function pickImage(cb) {
    var input = document.createElement("input");
    input.type = "file"; input.accept = "image/*";
    input.onchange = function () { var f = input.files && input.files[0]; if (f) cb(f); };
    input.click();
  }
  function shrink(file, maxDim, q, cap) {
    // 先用 createImageBitmap；很大的图在手机上它会解不开，退回 <img> 再试一次
    var viaTag = function () {
      return new Promise(function (ok, no) {
        var u = URL.createObjectURL(file), im = new Image();
        im.onload = function () { URL.revokeObjectURL(u); ok(im); };
        im.onerror = function () { URL.revokeObjectURL(u); no(new Error("decode")); };
        im.src = u;
      });
    };
    return (window.createImageBitmap ? createImageBitmap(file).catch(viaTag) : viaTag()).then(function (bmp) {
      var tryOnce = function (dim, qq) {
        var k = Math.min(1, dim / Math.max(bmp.width, bmp.height));
        var cv = document.createElement("canvas"); cv.width = Math.round(bmp.width * k); cv.height = Math.round(bmp.height * k);
        cv.getContext("2d").drawImage(bmp, 0, 0, cv.width, cv.height);
        return new Promise(function (r) { cv.toBlob(r, "image/jpeg", qq); }).then(function (b) {
          // 反向代理限制请求体 256KB（examples/Caddyfile）：超了先降画质，降到 0.6 还超再缩尺寸
          if (b && cap && b.size > cap && (qq > 0.6 || dim > 900)) return qq > 0.6 ? tryOnce(dim, qq - 0.08) : tryOnce(Math.round(dim * 0.8), q);
          return b;
        });
      };
      return tryOnce(maxDim, q);
    });
  }
  // 传图失败时给人看的话：浏览器和服务器的英文原话不直接甩出来
  function upErr(e) {
    var m = String((e && e.message) || "");
    if (/decod|not supported|InvalidState/i.test(m + " " + ((e && e.name) || ""))) return "这张图浏览器打不开，可能太大或格式不认，换一张 jpg / png 试试";
    if (/413|too large|payload/i.test(m)) return "图太大了，服务器不收，换一张小一点的";
    if (/failed to fetch|networkerror|load failed/i.test(m)) return "网络断了一下，再试一次";
    return /[\u4e00-\u9fff]/.test(m) ? m : "再试一次" + (m ? "（" + m + "）" : "");
  }
  function changeAvatar(who) {
    if (!PF) return;
    pickImage(function (f) {
      toast("图在缩…");
      shrink(f, 300, 0.85, 235 * 1024).then(function (b) { return PF.upload(who, b); })
        .then(function (p) { onPrefs(p); toast("换好了"); })
        .catch(function (e) { toast("没传上：" + upErr(e)); });
    });
  }

  // ══ 推荐页（顶上三个标签：推荐 / 热门 / 直播；下拉或点底部按钮换一批） ══
  var R = { mode: "rcmd", fresh: 1, page: 1, q: "", items: [] };
  function rcmdRender() {
    var pg = $("pg-rcmd");
    var tabs = R.mode === "search"
      ? '<button class="tab on" type="button">' + esc(R.q) + '</button><span class="sp"></span><button class="tab icon" type="button" data-act="unsearch" aria-label="回推荐">' + I("x", 15, 1.8) + "</button>"
      : [["rcmd", "推荐"], ["hot", "热门"], ["live", "直播"]].map(function (x) {
          return '<button type="button" data-mode="' + x[0] + '" class="tab' + (R.mode === x[0] ? " on" : "") + '">' + x[1] + "</button>";
        }).join("");
    pg.innerHTML = '<div class="tabs">' + tabs + '</div><div id="rcmdList">' + (R.mode === "live" ? "" : skeleton(4)) + "</div>";
    pg.querySelectorAll("[data-mode]").forEach(function (b) {
      b.onclick = function () { if (R.mode !== b.dataset.mode) { R.mode = b.dataset.mode; R.page = 1; $("body").scrollTop = 0; rcmdRender(); } };
    });
    var u = pg.querySelector('[data-act="unsearch"]');
    if (u) u.onclick = function () { $("searchIn").value = ""; $("searchX").classList.add("hidden"); R.mode = "rcmd"; rcmdRender(); };
    return R.mode === "live" ? liveRender($("rcmdList")) : rcmdLoad(false);
  }
  // 推荐 / 热门这一批记在 sessionStorage（10 分钟）：点进去看完一返回，这一页原样回来、停在原来滑到的地方，
  // 不用再等一趟 B 站、也不会换一批（封面也都在浏览器缓存里）。下拉刷新照旧换一批。
  var LC_KEY = "bilitube_list_cache", LC_MS = 600000;
  function lcRead(mode) {
    try {
      var c = JSON.parse(sessionStorage.getItem(LC_KEY) || "null");
      if (c && c.mode === mode && Date.now() - c.at < LC_MS && c.items && c.items.length) return c;
    } catch (e) {}
    return null;
  }
  function lcWrite() {
    if (R.mode !== "rcmd" && R.mode !== "hot") return;
    try { sessionStorage.setItem(LC_KEY, JSON.stringify({ mode: R.mode, at: Date.now(), items: R.items.slice(0, 60), fresh: R.fresh, page: R.page, more: R.more, y: body.scrollTop })); } catch (e) {}
  }
  function lcDrop() { try { sessionStorage.removeItem(LC_KEY); } catch (e) {} }
  window.addEventListener("pagehide", function () { if (curPage === "rcmd") lcWrite(); });
  function rcmdLoad(more, fromCache) {
    var mode = R.mode, hit = !more && fromCache !== false && !DEMO ? lcRead(mode) : null;
    var req = hit ? Promise.resolve({ data: { list: hit.items, has_more: hit.more } })
      : mode === "rcmd" ? get("recommend", { fresh: R.fresh })
      : mode === "hot" ? get("popular", { page: R.page })
      : get("search", { q: R.q, page: R.page });
    if (hit) { R.fresh = hit.fresh || R.fresh; R.page = hit.page || R.page; }
    return req.then(function (r) {
      if (mode !== R.mode) return;
      var list = (r.data && r.data.list) || [];
      if (!more) R.items = [];
      var start = R.items.length;
      R.items = R.items.concat(list);
      if (!hit && (mode === "rcmd" || mode === "hot")) { R.more = !(r.data && r.data.has_more === false); if (!r.demo) lcWrite(); }
      if (hit) setTimeout(function () { if (curPage === "rcmd" && R.mode === mode) body.scrollTop = hit.y || 0; }, 0);
      var box = $("rcmdList");
      if (!R.items.length) { box.innerHTML = '<div class="empty-line">' + (mode === "search" ? "没搜到" : "没拿到，往下拉一下再试") + "</div>"; return; }
      var canMore = mode === "rcmd" || (r.data && r.data.has_more !== false && list.length);
      if (!more) {
        box.innerHTML = (r.demo ? '<div class="demo-row">' + demoTag(r) + "</div>" : "") + (mode === "search" ? '<div id="pgcHits"></div>' : "") +
          '<div class="grid2" id="rcmdGrid">' + R.items.map(function (it, i) { return cardHTML(it, i); }).join("") + "</div>" + (canMore ? moreBtn() : "");
        if (mode === "search" && !r.demo) pgcHits(R.q);
      } else {
        $("rcmdGrid").insertAdjacentHTML("beforeend", list.map(function (it, k) { return cardHTML(it, start + k, k); }).join(""));
        if (!canMore) { var mr = box.querySelector(".more-row"); if (mr) mr.remove(); }
      }
      bindPlay(box, R.items);
      var m = box.querySelector("[data-more]");
      if (m) m.onclick = function () { if (mode === "rcmd") R.fresh++; else R.page++; m.disabled = true; rcmdLoad(true); setTimeout(function () { m.disabled = false; }, 800); };
    });
  }
  // 搜索结果里的番剧 / 影视：一行一部（封面、名字、类型 · 集数、角标），点击进入整部（由后端定位到上次看到的那一集）
  function pgcLines(it) {
    return [esc([it.type_name, it.index_show || (it.eps ? it.eps + " 集" : "")].filter(Boolean).join(" · ")),
      esc(it.progress || it.badge || it.areas || "")];
  }
  function pgcHits(q) {
    get("pgcsearch", { q: q }, null, { noFake: true }).then(function (r) {
      var el = $("pgcHits"); if (!el || R.mode !== "search" || R.q !== q) return;
      var d = r.data || {}, list = (d.bangumi || []).concat(d.ft || []);
      if (!list.length) return;
      el.innerHTML = '<div class="sec"><span>番剧 · 影视</span></div><div class="blk flush"><div class="rows">' +
        list.map(function (it, i) { return rowHTML(it, i, pgcLines(it), ""); }).join("") + '</div></div><div class="sec"><span>视频</span></div>';
      bindPlay(el, list);
    });
  }
  // 搜索：左边的放大镜就是提交按钮，回车也行
  $("searchForm").onsubmit = function (e) {
    e.preventDefault();
    var q = $("searchIn").value.trim(); if (!q) { $("searchIn").focus(); return; }
    $("searchIn").blur(); R.mode = "search"; R.q = q; R.page = 1; go("rcmd"); rcmdRender();
  };
  $("searchIn").oninput = function () { $("searchX").classList.toggle("hidden", !$("searchIn").value); };
  $("searchX").onclick = function () { $("searchIn").value = ""; $("searchX").classList.add("hidden"); if (R.mode === "search") { R.mode = "rcmd"; rcmdRender(); } else $("searchIn").focus(); };

  // ══ 直播（推荐页第三个标签）：输房间号进直播间＋关注的人谁在播。进直播间走 config.openLive（播放页 ?film=live:<房间号>） ══
  function faceUrl(u) { return u.face && u.mid ? apiUrl("face", { mid: u.mid, u: u.face }) : ""; }
  function upName(s) { return String(s || "").replace(/^示例UP·/, ""); }
  function liveRender(box) {
    box.innerHTML =
      '<div class="blk stagger-in"' + sg(0) + '><form class="room-in" id="roomForm" autocomplete="off">' + I("tv", 16, 1.8) +
        '<input id="roomIn" class="search-input" inputmode="numeric" enterkeyhint="go" placeholder="输房间号进直播间" aria-label="房间号">' +
        '<button class="room-go" type="submit" aria-label="进直播间">' + I("arrow", 16, 2) + "</button></form></div>" +
      '<div class="sec stagger-in"' + sg(1) + '><span>关注的人在播</span></div><div id="liveList"><div class="blk"><div class="sk" style="height:40px"></div></div></div>';
    $("roomForm").onsubmit = function (e) {
      e.preventDefault();
      var m = /(\d{1,12})\s*$/.exec(($("roomIn").value || "").replace(/[?#].*$/, "").replace(/\/+$/, ""));
      if (!m) { toast("房间号是一串数字"); $("roomIn").focus(); return; }
      C.openLive(m[1]);
    };
    return get("live").then(function (r) {
      var el = $("liveList"); if (!el) return;
      var list = ((r.data && r.data.list) || []).filter(function (u) { return u.room; });
      if (!list.length) { el.innerHTML = '<div class="empty-line">' + (r.demo && r.err ? "没拿到" : "关注的人现在都没在播") + "</div>"; return; }
      el.innerHTML = '<div class="blk lv-list">' + (r.demo ? '<div class="demo-row" style="padding:0 0 8px">' + demoTag(r) + "</div>" : "") + list.map(function (u, i) {
        var f = faceUrl(u);
        return '<div class="lv stagger-in" role="button" tabindex="0" data-room="' + esc(u.room) + '"' + sg(i + 2) + ">" +
          '<span class="av' + (f ? "" : " ph") + '"' + (f ? " style=\"background-image:url('" + esc(f) + "')\"" : "") + ">" + (f ? "" : I("user", 20, 1.5)) + "</span>" +
          '<span class="tx"><b>' + esc(upName(u.uname)) + "</b><span>" + esc([u.title, u.area].filter(Boolean).join(" · ")) + "</span></span>" + I("right", 15, 1.75) + "</div>";
      }).join("") + "</div>";
      el.querySelectorAll("[data-room]").forEach(function (b) { b.onclick = function () { C.openLive(b.dataset.room); }; });
    });
  }

  // ══ 关注（参照 B 站 App 的「关注」：一排 UP 主名字，正在直播的摘要，下面是一条条投稿动态） ══
  var F = { host: null, hostName: null, ups: [], all: [] };
  function followRender() {
    var pg = $("pg-follow");
    pg.innerHTML = '<div class="tabs" id="upBox"></div><div id="liveBox"></div><div id="feedBox">' + '<div class="post"><div class="sk" style="height:180px"></div></div>' + "</div>";
    get("live").then(function (r) {
      var list = ((r.data && r.data.list) || []).filter(function (u) { return u.room; });
      if (!list.length) { $("liveBox").innerHTML = ""; return; }
      $("liveBox").innerHTML = '<div class="blk stagger-in" style="margin-bottom:8px">' + list.map(function (u) {
        var f = faceUrl(u), fa = f ? '<span class="fa" style="background-image:url(\'' + esc(f) + '\')"></span>' : "";
        return '<div class="live-sum" role="button" tabindex="0" data-room="' + esc(u.room) + '">' + fa + '<span class="k">直播</span>' +
          '<span class="tx"><b>' + esc(upName(u.uname)) + "</b><span>" + esc(u.title || "") + "</span></span>" + I("right", 15, 1.75) + "</div>";
      }).join("") + (r.demo ? '<div style="margin-top:8px">' + demoTag(r) + "</div>" : "") + "</div>";
      $("liveBox").querySelectorAll("[data-room]").forEach(function (b) { b.onclick = function () { C.openLive(b.dataset.room); }; });
    });
    return followLoad();
  }
  function followLoad() {
    var host = F.host;
    var req = host && !DEMO ? get("dynamic", { page: 1 }, { host_mid: host }) : get("dynamic", { page: 1 });
    return req.then(function (r) {
      if (host !== F.host) return;
      var list = (r.data && r.data.list) || [];
      if (!host) {
        F.all = list;
        var seen = {}, ups = [];
        list.forEach(function (it) {
          var k = it.up_mid || it.up;
          if (!seen[k]) { seen[k] = { mid: it.up_mid, name: it.up }; ups.push(seen[k]); }
        });
        F.ups = ups;
      } else if (DEMO || r.demo) {
        list = F.all.filter(function (it) { return String(it.up_mid) === String(host); });
      }
      // 从 AI 主页的「关注」点进来的那位，最近没投稿时上面那排没有 TA：补在最前面
      if (host && F.hostName && !F.ups.some(function (u) { return String(u.mid) === String(host); })) F.ups.unshift({ mid: host, name: F.hostName });
      $("upBox").innerHTML = '<button class="tab' + (!host ? " on" : "") + '" type="button" data-mid="">全部</button>' +
        F.ups.map(function (u) {
          return '<button class="tab' + (String(host) === String(u.mid) ? " on" : "") + '" type="button" data-mid="' + esc(u.mid) + '">' + esc(upName(u.name)) + "</button>";
        }).join("");
      $("upBox").querySelectorAll("[data-mid]").forEach(function (b) {
        b.onclick = function () { var m = b.dataset.mid || null; if (String(m) === String(F.host)) return; F.host = m; followLoad(); };
      });
      $("feedBox").innerHTML = (r.demo ? '<div class="demo-row">' + demoTag(r) + "</div>" : "") + (list.length ? list.map(function (it, i) {
        var left = (it.duration ? "<span>" + esc(fmtDur(it.duration)) + "</span>" : "") + (fmtPlay(it.play) ? "<span>" + esc(fmtPlay(it.play)) + "播放</span>" : "");
        return '<div class="post stagger-in" data-i="' + i + '" role="button" tabindex="0"' + sg(i) + '><div class="who">' + esc(upName(it.up)) + '</div><div class="when">' + esc(it.pubdate || "") + " · 投稿了视频</div>" +
          cvHTML(it, left, "") + '<div class="t">' + esc(it.title) + "</div></div>";
      }).join("") : '<div class="empty-line">没有新投稿</div>');
      bindPlay($("feedBox"), list);
    });
  }

  // ══ 聊过：GET /dm/log 列出有私人弹幕的片 → 逐部取 /dm/log?film=；封面和 UP 主从后端认得的片子（/films）取 ══
  var TK = null;
  function talkedData() {
    if (TK) return Promise.resolve(TK);
    if (DEMO) return Promise.resolve(TK = { list: D.talked.list, src: "demo" });
    return Promise.all([get("talkedList", null, null, { noFake: true }), get("films", null, null, { noFake: true })]).then(function (rs) {
      var fl = (rs[0].data && rs[0].data.films) || [];
      if (!rs[0].data) return (TK = { list: D.talked.list, src: "demo" });
      var shelf = {}; (((rs[1].data || {}).films) || []).forEach(function (f) { shelf[f.id] = f; });
      return Promise.all(fl.map(function (x) {
        return get("talkedFilm", { film: x.film }, null, { noFake: true }).then(function (r) {
          var f = shelf[x.film] || {}, items = (r.data && r.data.items) || [];
          var bv = /^bili:(BV[0-9A-Za-z]{10})/.exec(x.film || "");
          if (x.title === x.film) x.title = "";   // 记录里没存片名时，后端会用片子 id 代替
          if (!x.title && !f.title && bv) {   // 后端已不认得、记录里也没存片名：向 B 站查一下标题
            return get("pages", { bvid: bv[1] }, null, { noFake: true }).then(function (pr) {
              x.title = (pr.data && pr.data.title) || bv[1]; return [f, items];
            });
          }
          return [f, items];
        }).then(function (fi) {
          var f = fi[0], items = fi[1];
          // 封面只用后端认得的；不认得的片不留封面位，免得画一块空灰
          return { film: x.film, bvid: f.bvid, title: x.title || f.title || x.film, up: f.up, duration: f.duration,
            poster: f.poster || "", noCover: !f.poster,
            lines: items.map(function (l) { var a = l.author || "user"; return { t: l.t, who: a === "user" ? "me" : "ai", author: a, text: l.text, at: l.at }; }) };
        });
      })).then(function (list) {
        return (TK = { list: list.filter(function (x) { return x.lines.length; }), src: "api" });
      });
    });
  }
  function lastAt(x) { var a = ""; (x.lines || []).forEach(function (l) { if ((l.at || "") > a) a = l.at || ""; }); return a; }
  function sortTk(list) { return list.slice().sort(function (a, b) { return lastAt(b) < lastAt(a) ? -1 : 1; }); }
  function tkCardHTML(x, i, cap) {
    // 直播没有时间码：按两个人发出去的先后排，时间钮显示几点几分；录播按片子里的秒数排
    var isLive = /^live:/.test(String(x.film || ""));
    var lines = (x.lines || []).slice().sort(isLive
      ? function (a, b) { return (a.at || "") < (b.at || "") ? -1 : (a.at || "") > (b.at || "") ? 1 : 0; }
      : function (a, b) { return (a.t || 0) - (b.t || 0) || (a.who === "me" ? -1 : 1); });
    var hidden = cap && lines.length > cap ? lines.length - cap : 0;
    var prevT = null;
    var ls = lines.map(function (l, k) {
      var hm = isLive ? local(l.at).hm : "";
      var same = isLive ? (prevT !== null && hm === prevT) : (prevT !== null && Math.abs((l.t || 0) - prevT) < 1);
      prevT = isLive ? hm : (l.t || 0);
      return '<div class="ln ' + (l.who === "me" ? "me" : "ai") + '"' + (hidden && k >= cap ? " hidden" : "") + '><button class="ts' + (same ? " same" : "") + '" type="button" data-i="' + i + '" data-t="' + (l.t || 0) + '">' + (isLive ? hm : fmtT(l.t)) + "</button>" +
        '<span class="tx">' + (l.who === "me" ? "" : '<span class="who" data-nm="ai">' + esc(aiName()) + "</span>") + esc(l.text) + "</span></div>";
    }).join("");
    var lt = local(lastAt(x));
    var meta = [niceDay(lt.day), x.up].filter(Boolean).join(" · ");
    return '<div class="card stagger-in"' + sg(i) + '><div class="tk-film" data-film="' + i + '" role="button" tabindex="0">' + (x.noCover ? "" : cvHTML(x)) +
      '<div class="tx"><div class="t">' + esc(x.title) + '</div><div class="m">' + esc(meta) + "</div></div></div>" +
      '<div class="tk-lines">' + ls + (hidden ? '<button class="tab more-lines" type="button" data-unfold style="align-self:flex-start;margin-left:50px">+' + hidden + "</button>" : "") + "</div></div>";
  }
  // 封面图取不到（404、断网）就把整个封面位拿掉，只留片名和弹幕
  function dropDeadCovers(root) {
    root.querySelectorAll(".tk-film .cv:not(.ph)").forEach(function (el) {
      var m = /url\('([^']+)'\)/.exec(el.getAttribute("style") || ""); if (!m) return;
      var im = new Image(); im.onerror = function () { el.remove(); }; im.src = m[1];
    });
  }
  function bindTk(root, list) {
    dropDeadCovers(root);
    root.querySelectorAll("[data-film]").forEach(function (el) { el.onclick = function () { play(list[+el.dataset.film]); }; });
    root.querySelectorAll(".ts").forEach(function (el) { el.onclick = function () { play(list[+el.dataset.i], +el.dataset.t); }; });
    root.querySelectorAll("[data-unfold]").forEach(function (b) { b.onclick = function () { b.parentNode.querySelectorAll(".ln[hidden]").forEach(function (x) { x.hidden = false; }); b.remove(); }; });
  }
  function monthOf(day) { var m = String(day || "").match(/^(\d{4})-(\d{2})/); return m ? (+m[2]) + " 月" : ""; }
  // 「聊过」页按关键词找：/dm/log 是一次全拿回来的（片子一览＋每部的全部私人弹幕，不分页），所以只在页面里过滤。
  // 空格隔开的几个词都要沾上（片名 / UP / 片子 id / 两个人的弹幕，哪处都算）；弹幕里命中的，片名下面先露最近的一两句并标出关键词，
  // 原来那几行收进「全部 N 句」。
  var TKQ = "";
  function tkTerms(q) { return String(q || "").toLowerCase().split(/\s+/).filter(Boolean); }
  function tkHas(s, t) { return String(s || "").toLowerCase().indexOf(t) >= 0; }
  function tkHitLines(x, terms) { return (x.lines || []).filter(function (l) { return terms.some(function (t) { return tkHas(l.text, t); }); }); }
  function tkMatch(x, terms) {
    var head = [x.title, x.up, x.film, x.bvid].join("\n");
    return terms.every(function (t) { return tkHas(head, t) || (x.lines || []).some(function (l) { return tkHas(l.text, t); }); });
  }
  function tkHitsHTML(x, i, hits) {
    var isLive = /^live:/.test(String(x.film || ""));
    var show = hits.slice().sort(function (a, b) { return (a.at || "") < (b.at || "") ? 1 : -1; }).slice(0, 2);   // 最近说的那两句
    return '<div class="tk-lines tk-hits">' + show.map(function (l) {
      return '<div class="ln ' + (l.who === "me" ? "me" : "ai") + '"><button class="ts" type="button" data-i="' + i + '" data-t="' + (l.t || 0) + '">' + (isLive ? local(l.at).hm : fmtT(l.t)) + "</button>" +
        '<span class="tx">' + (l.who === "me" ? "" : '<span class="who" data-nm="ai">' + esc(aiName()) + "</span>") + esc(l.text) + "</span></div>";
    }).join("") + (hits.length > show.length ? '<div class="tk-hits-n">还有 ' + (hits.length - show.length) + " 句也说到</div>" : "") + "</div>";
  }
  // 标出关键词：只动片名、UP 那行和命中的那几句里的字，AI 的署名不标
  function tkMark(root, terms) {
    if (!terms.length) return;
    root.querySelectorAll(".tk-film .t, .tk-film .m, .tk-hits .tx").forEach(function (el) {
      var w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null), nodes = [], n;
      while ((n = w.nextNode())) if (!(n.parentNode.closest && n.parentNode.closest(".who"))) nodes.push(n);
      nodes.forEach(function (node) {
        var s = node.nodeValue, low = s.toLowerCase(), marks = [];
        terms.forEach(function (t) { for (var k = low.indexOf(t); k >= 0; k = low.indexOf(t, k + t.length)) marks.push([k, k + t.length]); });
        if (!marks.length) return;
        marks.sort(function (a, b) { return a[0] - b[0]; });
        var frag = document.createDocumentFragment(), at = 0;
        marks.forEach(function (m) {
          if (m[1] <= at) return;
          var a = Math.max(m[0], at);
          if (a > at) frag.appendChild(document.createTextNode(s.slice(at, a)));
          var mk = document.createElement("mark"); mk.className = "hl"; mk.textContent = s.slice(a, m[1]); frag.appendChild(mk);
          at = m[1];
        });
        if (at < s.length) frag.appendChild(document.createTextNode(s.slice(at)));
        node.parentNode.replaceChild(frag, node);
      });
    });
  }
  function tkDraw(box, d, all) {
    var terms = tkTerms(TKQ), list = terms.length ? all.filter(function (x) { return tkMatch(x, terms); }) : all;
    if (!list.length) { box.innerHTML = '<div class="empty-line">没找到带「' + esc(TKQ.trim()) + "」的片</div>"; return; }
    var html = d.src === "demo" ? '<div class="demo-row">' + demoTag({ demo: true }) + "</div>" : "", lastM = null;
    list.forEach(function (x, i) {
      var mth = monthOf(local(lastAt(x)).day);
      if (mth !== lastM) { html += '<div class="mo">' + esc(mth) + "</div>"; lastM = mth; }
      var card = tkCardHTML(x, i, 4), hits = terms.length ? tkHitLines(x, terms) : [];
      if (terms.length) card = card.replace("card stagger-in", "card");   // 边打字边过滤时不要每次都重新飘进来
      if (hits.length) {   // 原来那几行原样留着，先收起来（命中的一两句就是全部的话，不给「全部」按钮）
        var more = x.lines.length > Math.min(hits.length, 2);
        card = card.replace('<div class="tk-lines">', tkHitsHTML(x, i, hits) + (more ? '<button class="tab more-lines tk-all" type="button" data-allines>全部 ' + x.lines.length + " 句</button>" : "") + '<div class="tk-lines" hidden>');
      }
      // 横滑删除（和「稍后再看」同一套 swipeList）：卡片外面包一层 .sw，底下垫删除；桌面上右上角有一颗不显眼的小叉
      var at = card.indexOf(">") + 1;
      card = card.slice(0, at) + '<button class="sw-x" type="button" title="删掉这部片下面的私人弹幕" aria-label="删掉这部片下面的私人弹幕">' + I("x", 13, 2) + "</button>" + card.slice(at);
      html += '<div class="sw tk-sw" data-k="' + i + '"><div class="sw-act" aria-hidden="true"><span class="sw-ic">' + I("trash", 20, 1.8) + "</span></div>" + card + "</div>";
    });
    box.innerHTML = '<div class="tk-cards">' + html + "</div>";
    var cards = box.firstChild;
    bindTk(cards, list);
    cards.querySelectorAll("[data-allines]").forEach(function (b) { b.onclick = function () { b.nextElementSibling.hidden = false; b.remove(); }; });
    tkMark(cards, terms);
    swipeList(cards, function (k) { return tkForget(cards, d, all, list[k], k); }, ".card");
  }
  // 删一部片下面两个人的私人弹幕：先确认一次；删了先从列表里拿掉（底下 4 秒「撤销」），然后才真发 POST /dm/log/trash（后端软删除，挪进回收）。
  // 后端是旧版本（回「没有这条路」）或者别的原因没删成：卡片原样放回去，说一句人话，不假装删掉。
  function tkForget(cards, d, all, x, k) {
    if (!x) return null;
    var n = (x.lines || []).length;
    if (!window.confirm("删掉这部片下面我们聊过的 " + n + " 条弹幕？片子本身不动。\n\n" + (x.title || x.film))) return null;
    var el = cards.querySelector('.sw[data-k="' + k + '"]'), parent = el && el.parentNode, next = el && el.nextSibling;
    var drop = function () {   // 真删成了：从数据里拿掉，空下来的月份标题一起收
      [d.list, all].forEach(function (a) { var j = a.indexOf(x); if (j >= 0) a.splice(j, 1); });
      if (parent) parent.querySelectorAll(".mo").forEach(function (mo) { var nx = mo.nextElementSibling; if (!nx || nx.classList.contains("mo")) mo.remove(); });
      if (TK && TK.list && TK.list !== d.list) { var j = TK.list.indexOf(x); if (j >= 0) TK.list.splice(j, 1); }
    };
    var back = function (msg) {
      toast(msg);
      if (!el || !parent || !parent.isConnected) return;
      el.classList.remove("gone", "open", "l", "r", "go");
      var c = el.querySelector(".card"); if (c) c.style.transform = "";
      parent.insertBefore(el, next && next.parentNode === parent ? next : null);
    };
    return { label: "删掉了 " + n + " 条弹幕", commit: function () {
      if (DEMO || d.src === "demo") { drop(); return; }   // 示例数据不真删
      post("dmTrash", { film: x.film }).then(drop).catch(function (e) {
        var m = (e && e.message) || "";
        if (/下面没有私人弹幕/.test(m)) { drop(); return; }   // 别处已经删过了
        if (/没有这条路|HTTP 404/.test(m)) back("还删不了：后端还是旧版本，重启一下后端再试，这部片先留着");
        else if (e instanceof TypeError) back("没连上后端，没删掉，这部片先留着");
        else back("没删掉" + (m ? "：" + m : "") + "，这部片先留着");
      });
    } };
  }
  function talkedRender() {
    var pg = $("pg-talked");
    pg.innerHTML = '<div class="card"><div class="sk" style="height:90px"></div></div>';
    return talkedData().then(function (d) {
      var list = sortTk(d.list);
      if (!list.length) { pg.innerHTML = '<div class="empty-line">还没在哪部片里停下来聊过</div>'; return; }
      pg.innerHTML = '<form class="tk-search search-bar" id="tkSearch" role="search" autocomplete="off">' +
        '<button class="search-btn" type="submit" aria-label="找">' + I("search", 16, 2) + "</button>" +
        '<input class="search-input" id="tkIn" type="search" placeholder="找片名、UP、我们说过的话" aria-label="在聊过的片里找" enterkeyhint="search">' +
        '<button class="search-clear' + (TKQ ? "" : " hidden") + '" id="tkX" type="button" aria-label="清掉">' + I("x", 14, 2) + "</button></form>" +
        '<div id="tkList"></div>';
      var box = $("tkList"), inp = $("tkIn"), timer = null;
      inp.value = TKQ;
      var apply = function () { clearTimeout(timer); timer = null; if (inp.value === TKQ) return; TKQ = inp.value; tkDraw(box, d, list); };
      inp.oninput = function () { $("tkX").classList.toggle("hidden", !inp.value); clearTimeout(timer); timer = setTimeout(apply, 200); };   // 防抖：停手 0.2 秒才重画
      $("tkSearch").onsubmit = function (e) { e.preventDefault(); apply(); inp.blur(); };
      $("tkX").onclick = function () { inp.value = ""; $("tkX").classList.add("hidden"); apply(); inp.focus(); };
      tkDraw(box, d, list);
    });
  }

  // ══ 我的（参照 B 站 App 的「我的」：头像在左、名字在右，一排数字，图标宫格，最下面是 AI 的入口卡） ══
  function meRender() {
    var pg = $("pg-me");
    var gi = function (icon, label, act, k) { return '<button class="gi stagger-in" type="button" data-act="' + act + '"' + sg(k) + ">" + I(icon, 24, 1.5) + "<span>" + label + "</span></button>"; };
    pg.innerHTML =
      '<div class="blk stagger-in"' + sg(0) + '><div class="me-top">' + avHTML("user", "me-av", 26, "button") + '<div class="me-who"><div class="me-name">' + nm("user") + '</div><div class="me-sub" id="meSub">　</div></div></div>' +
      '<div class="nums" id="meNums"></div></div>' +
      '<div class="blk"><div class="grid4">' + gi("history", "历史记录", "hist", 1) + gi("star", "收藏夹", "fav", 2) + gi("clock", "稍后再看", "later", 3) + gi("tv", "追番", "pgc", 4) +
        (EMBED ? gi("users", "关注", "follow", 5) : "") + "</div></div>" +   // 嵌入模式没有底栏：「关注」从这儿进（「聊过」在上面那排数字里）
      (CP ? '<div class="blk pal-card stagger-in" role="button" tabindex="0" id="palCard"' + sg(5) + '><div class="blk-hd">' + avHTML("companion", "sm", 15) + '<span class="t">' + nm("ai") + '</span><span class="more" id="palN">' + I("right", 13, 1.8) + '</span></div><div class="row3" id="palPeek"></div></div>' : "");
    pg.querySelector(".me-av").onclick = function (e) { e.stopPropagation(); changeAvatar("user"); };
    if (CP) {
      $("palCard").onclick = function () { go("companion"); };
      pocket().then(function (pk) {
        $("palN").innerHTML = (pk.f ? "收藏夹里 " + esc(pk.f.count || 0) + " 个" : "") + I("right", 13, 1.8);
        var box = $("palPeek");
        if (!pk.f) { box.outerHTML = '<div class="pal-empty">还没建「' + esc(CP.favFolderTitle || "") + '」这个收藏夹，点进来看怎么建</div>'; return; }
        if (!+pk.f.count) { box.outerHTML = '<div class="pal-empty">收藏夹还空着</div>'; return; }
        get("favpeek", { id: pk.f.id }).then(function (x) {
          box.innerHTML = ((x.data && x.data.list) || []).slice(0, 3).map(function (it) { return cvHTML(it, "", "", true); }).join("");
        });
      });
    }
    pg.querySelectorAll("[data-act]").forEach(function (b) { b.onclick = function () { go(b.dataset.act); }; });
    var n = { fav: "–", later: "–", talked: "–" };
    var draw = function () {
      $("meNums").className = "nums n3";
      $("meNums").innerHTML = [["fav", "收藏"], ["later", "稍后再看"], ["talked", "聊过"]].map(function (x) {
        return '<button class="num" type="button" data-go="' + x[0] + '"><b>' + esc(n[x[0]]) + "</b><span>" + x[1] + "</span></button>";
      }).join("");
      $("meNums").querySelectorAll("[data-go]").forEach(function (b) { b.onclick = function () { go(b.dataset.go); }; });
    };
    draw();
    paintPeople(pg);
    var mp = loadMe().then(function () {
      $("meSub").innerHTML = ME.logged_in ? "和 " + nm("ai") + " 一起看" : ME._err ? "B 站问不到" : "B 站没登录";
      paintPeople(pg);
    });
    talkedData().then(function (d) { n.talked = String(d.list.length); draw(); });
    get("watchlater", { offset: 0 }).then(function (r) { n.later = String(r.data && r.data.total != null ? r.data.total : ((r.data && r.data.list) || []).length); draw(); });
    pocket().then(function (pk) { var t = 0; pk.mine.forEach(function (f) { t += +f.count || 0; }); n.fav = String(t); draw(); });   // 收藏数不含 AI 的收藏夹
    // 待看的单子只有一份＝B 站账号的稍后再看。服务器上存着的本地片（B 站单子装不了）留一个不起眼的入口，有才露
    get("films", null, null, { noFake: true }).then(function (r) {
      var k = (((r.data && r.data.films) || []).filter(function (f) { return f.kind !== "bili"; })).length;
      if (!k || !pg.isConnected) return;
      pg.insertAdjacentHTML("beforeend", '<div class="local-link"><button type="button" data-go="films">本地片 ' + k + " 部" + I("right", 12, 1.8) + "</button></div>");
      pg.querySelector('.local-link [data-go]').onclick = function () { go("films"); };
    });
    return mp;
  }

  // ══ AI 的收藏夹：按 config.companion 从用户的收藏夹里认出一个（填了 favFolderId 认 id，否则认 favFolderTitle） ══
  // 返回 { f: AI 的那个收藏夹或 null, mine: 其余的（用户自己的）, all, demo, err }
  var PK = null;
  function pocket() {
    if (PK) return PK;
    return (PK = get("folders").then(function (r) {
      var all = (r.data && r.data.list) || [], f = null;
      if (CP && (CP.favFolderId || CP.favFolderTitle)) {
        f = all.filter(function (x) { return CP.favFolderId ? String(x.id) === String(CP.favFolderId) : x.title === CP.favFolderTitle; })[0] || null;
      }
      return { f: f, mine: all.filter(function (x) { return x !== f; }), all: all, demo: !!r.demo, err: r.err };
    }));
  }
  function isAI(l) { return l.author ? l.author === AI_KEY : l.who === "ai"; }

  // ══ AI 的主页：头像＋名字＋签名、一排数字、AI 的收藏夹（两列卡）、AI 发过的弹幕 ══
  function companionRender() {
    var pg = $("pg-sub");
    pg.innerHTML =
      '<div class="blk pal-top stagger-in"' + sg(0) + '><div class="me-top">' + avHTML("companion", "me-av", 26, "button") + '<div class="me-who"><div class="me-name">' + nm("ai") + '</div><div class="me-sub" id="palSub">　</div></div></div><div class="nums n3" id="palNums"></div></div>' +
      '<div class="sec stagger-in" id="palSecPk"' + sg(1) + '><span>收藏夹</span></div><div id="palGrid">' + skeleton(2) + "</div>" +
      '<div class="sec stagger-in"' + sg(2) + '><span>关注</span></div><div id="palFg"><div class="blk"><div class="sk" style="height:60px"></div></div></div>' +
      '<div class="sec stagger-in"' + sg(3) + '><span>弹幕</span></div><div class="blk" id="palDm"><div class="sk" style="height:60px"></div></div>';
    pg.querySelector(".me-av").onclick = function () { changeAvatar("companion"); };
    var n = { pk: "–", fg: "–", films: "–", dm: "–" };
    var draw = function () {
      $("palNums").className = "nums";
      $("palNums").innerHTML = '<div class="num"><b>' + esc(n.pk) + '</b><span>收藏夹</span></div>' +
        '<div class="num"><b>' + esc(n.fg) + '</b><span>关注</span></div>' +
        '<button class="num" type="button" data-go="talked"><b>' + esc(n.films) + '</b><span>聊过</span></button>' +
        '<div class="num"><b>' + esc(n.dm) + "</b><span>弹幕</span></div>";
      $("palNums").querySelector("[data-go]").onclick = function () { go("talked"); };
    };
    draw();
    paintPeople(pg);
    loadMe().then(function () { paintPeople(pg); });

    // 收藏夹：显示最近收藏的六个，更多的话标题右边出「全部」，进收藏夹页
    var p1 = pocket().then(function (pk) {
      var box = $("palGrid"); if (!box) return;
      if (!pk.f) { n.pk = "0"; draw(); box.innerHTML = '<div class="empty-line">这里放 AI 收藏的视频，现在还没有。在 B 站上新建一个收藏夹，名字取「' + esc((CP && CP.favFolderTitle) || "") + "」（建议设为私密），回来刷新就能看到。</div>"; return; }
      n.pk = String(pk.f.count || 0); draw();
      if (!+pk.f.count) { box.innerHTML = '<div class="empty-line">收藏夹还空着</div>'; return; }
      return get("favlist", { page: 1, id: pk.f.id }).then(function (r) {
        var list = ((r.data && r.data.list) || []).slice(0, 6);
        box.innerHTML = (r.demo ? '<div class="demo-row">' + demoTag(r) + "</div>" : "") + '<div class="grid2">' + list.map(function (it, i) { return cardHTML(it, i); }).join("") + "</div>";
        bindPlay(box, list);
        if (+pk.f.count > list.length) {
          $("palSecPk").insertAdjacentHTML("beforeend", '<button class="more" type="button">全部' + I("right", 13, 1.8) + "</button>");
          $("palSecPk").querySelector(".more").onclick = function () { parentOf.folder = "companion"; go("folder/" + pk.f.id); };
        }
      });
    });

    // 关注：AI 自己关注的 UP＝用户 B 站账号里那个专属关注分组（MCP 按 follow_group_id / follow_group_name 认）。
    // 头像＋名字，点了进「关注」页、只看这个人；没配分组或分组为空时写一句怎么弄
    var p3 = get("followGroup", { page: 1 }, null, { noFake: !DEMO }).then(function (r) {
      var box = $("palFg"); if (!box) return;
      var d = r.data || {}, g = d.group, list = d.list || [];
      if (r.err && !r.demo) {
        n.fg = "–"; draw();
        return loadMe().then(function () {
          var b2 = $("palFg"); if (!b2) return;
          b2.innerHTML = '<div class="empty-line">' + (ME && !ME.logged_in && !ME._err ? "B 站没登录，看不到" : "关注没拿到，往下拉一下再试") + "</div>";
        });
      }
      n.fg = g ? String(g.count != null ? g.count : list.length) : "0"; draw();
      if (!g) {
        var w = d.want || {};
        box.innerHTML = '<div class="empty-line">' + (w.id ? "配置里指定的关注分组（id " + esc(w.id) + "）在 B 站上没找到，看看是不是删了。"
          : "这里放 AI 关注的 UP，现在还没有。在 B 站的关注列表里新建一个分组，名字取「" + esc(w.name || "") + "」，回来刷新就能看到。") + "</div>";
        return;
      }
      if (!list.length) { box.innerHTML = '<div class="empty-line">还没关注谁</div>'; return; }
      box.innerHTML = (r.demo ? '<div class="demo-row">' + demoTag(r) + "</div>" : "") + '<div class="blk"><div class="fg-ups">' + list.map(function (u, i) {
        var f = faceUrl(u);
        return '<button class="fg-up stagger-in" type="button" data-k="' + i + '"' + sg(i) + '><span class="fa"' + (f ? " style=\"background-image:url('" + esc(f) + "')\"" : "") + '></span><span class="nm">' + esc(upName(u.uname)) + "</span></button>";
      }).join("") + "</div></div>";
      box.querySelectorAll("[data-k]").forEach(function (b) {
        b.onclick = function () { var u = list[+b.dataset.k]; F.host = String(u.mid); F.hostName = u.uname; delete drawn.follow; go("follow"); };
      });
    });

    // 弹幕：从「聊过」那份数据里挑出 AI 发的，取最近八条
    var p2 = talkedData().then(function (d) {
      var mine = [], films = 0;
      d.list.forEach(function (x) {
        var ls = (x.lines || []).filter(isAI);
        if (ls.length) films++;
        ls.forEach(function (l) { mine.push({ x: x, l: l }); });
      });
      n.films = String(films); n.dm = String(mine.length); draw();
      var box = $("palDm"); if (!box) return;
      if (!mine.length) { box.outerHTML = '<div class="empty-line">还没发过弹幕</div>'; return; }
      mine.sort(function (a, b) { return (b.l.at || "") < (a.l.at || "") ? -1 : 1; });
      mine = mine.slice(0, 8);
      box.innerHTML = (d.src === "demo" ? '<div class="demo-row" style="padding:0 0 8px">' + demoTag({ demo: true }) + "</div>" : "") + mine.map(function (m, i) {
        return '<div class="pdm stagger-in"' + sg(i) + '><button class="ts" type="button" data-k="' + i + '">' + fmtT(m.l.t) + '</button><div class="tx"><div class="s">' + esc(m.l.text) +
          '</div><div class="m">' + esc([m.x.title, niceDay(local(m.l.at).day)].filter(Boolean).join(" · ")) + "</div></div></div>";
      }).join("");
      box.querySelectorAll("[data-k]").forEach(function (b) { b.onclick = function () { var m = mine[+b.dataset.k]; play(m.x, m.l.t); }; });
    });
    return Promise.all([p1, p2, p3]);
  }

  // ══ 横滑删除（稍后再看 / 本地片）══
  // 往左滑、往右滑都行：滑过 64px 松手停在半开，露出删除按钮，点它才删；一口气滑过半行宽（或甩得够快）直接删。
  // 删了先从列表里拿掉、底部提示条给「撤销」，4 秒后才真正发请求（中途离开页面也会立即发出）。
  // 起手点在屏幕左右边缘 24px 以内的不接（留给浏览器 / 系统的边缘返回手势）；只认横向手势，竖向滑动交给页面滚动。
  // .sw .row 设了 touch-action: pan-y；不要在 pointerdown 里 preventDefault，否则会挡住页面滚动。
  var PEND = null;   // 还在撤销窗口里、等着真正删除的那一条 {commit, undo}
  function flushPending() { if (PEND) { var p = PEND; PEND = null; p.commit(); } }
  window.addEventListener("pagehide", flushPending);
  document.addEventListener("visibilitychange", function () { if (document.visibilityState === "hidden") flushPending(); });
  // rowSel：跟手移动的那一块，默认 .row（「聊过」页传 .card）；行里有 .sw-x（桌面上的小叉）的话，点它等于滑开再点删除
  function swipeList(root, onDelete, rowSel) {
    var openRow = null, SEL = rowSel || ".row";
    var closeOpen = function (except) {
      if (openRow && openRow !== except) { var r = openRow.querySelector(SEL); r.style.transition = ""; r.style.transform = ""; openRow.classList.remove("open"); openRow = null; }
    };
    var EDGE = 24, OPEN = 76;
    root.querySelectorAll(".sw").forEach(function (sw) {
      var row = sw.querySelector(SEL), st = null;
      var setX = function (x) { row.style.transform = x ? "translate3d(" + x + "px,0,0)" : ""; sw.classList.toggle("l", x > 0); sw.classList.toggle("r", x < 0); };
      row.addEventListener("pointerdown", function (e) {
        if (e.pointerType === "mouse" && e.button !== 0) return;
        if (e.clientX < EDGE || e.clientX > window.innerWidth - EDGE) return;
        closeOpen(sw);
        var base = sw.classList.contains("open") ? (sw.classList.contains("l") ? OPEN : -OPEN) : 0;
        st = { x0: e.clientX, y0: e.clientY, t0: performance.now(), dir: 0, base: base, dx: base, id: e.pointerId };
      });
      row.addEventListener("pointermove", function (e) {
        if (!st || e.pointerId !== st.id) return;
        var dx = e.clientX - st.x0, dy = e.clientY - st.y0;
        if (!st.dir) {
          if (Math.abs(dx) > 10 && Math.abs(dx) > Math.abs(dy) * 1.5) { st.dir = 1; row.style.transition = "none"; try { row.setPointerCapture(e.pointerId); } catch (er) {} }
          else if (Math.abs(dy) > 10) { st = null; return; }
          else return;
        }
        var w = sw.offsetWidth, x = st.base + dx;
        if (Math.abs(x) > w * 0.6) x = (x > 0 ? 1 : -1) * (w * 0.6 + (Math.abs(x) - w * 0.6) * 0.35);   // 超过六成行宽后加阻尼
        st.dx = x; setX(x);
        sw.classList.toggle("go", Math.abs(x) > w * 0.5);
      });
      var end = function (e) {
        if (!st || (e && e.pointerId !== st.id)) return;
        var s = st; st = null;
        if (!s.dir) return;
        row.dataset.swiped = "1"; setTimeout(function () { delete row.dataset.swiped; }, 350);   // 滑完松手触发的 click 不算点开
        row.style.transition = "";
        var w = sw.offsetWidth, v = (s.dx - s.base) / Math.max(1, performance.now() - s.t0);
        sw.classList.remove("go");
        if (Math.abs(s.dx) > w * 0.5 || (Math.abs(v) > 0.9 && Math.abs(s.dx) > OPEN)) { kill(sw, s.dx > 0 ? 1 : -1); return; }
        if (Math.abs(s.dx) > 64) { setX(s.dx > 0 ? OPEN : -OPEN); sw.classList.add("open"); openRow = sw; return; }
        setX(0); sw.classList.remove("open"); if (openRow === sw) openRow = null;
      };
      row.addEventListener("pointerup", end);
      row.addEventListener("pointercancel", function (e) { if (st && e.pointerId === st.id) { st = null; row.style.transition = ""; setX(0); sw.classList.remove("open", "go"); } });
      sw.querySelector(".sw-act").addEventListener("click", function () { if (sw.classList.contains("open")) kill(sw, sw.classList.contains("l") ? 1 : -1); });
      var sx = sw.querySelector(".sw-x");
      if (sx) sx.addEventListener("click", function (e) { e.stopPropagation(); e.preventDefault(); kill(sw, -1); });
      // 半开时点这一行＝合上，不去播放
      sw.addEventListener("click", function (e) {
        if (!row.contains(e.target)) return;
        if (row.dataset.swiped) { e.stopPropagation(); e.preventDefault(); return; }   // 刚滑完松手的那次 click
        if (sw.classList.contains("open")) { e.stopPropagation(); e.preventDefault(); closeOpen(); }
      }, true);
    });
    root.addEventListener("click", function (e) { if (openRow && !openRow.contains(e.target)) closeOpen(); }, true);
    function kill(sw, dir) {
      var row = sw.querySelector(SEL), k = +sw.dataset.k;
      var job = onDelete(k);
      if (!job) { row.style.transform = ""; sw.classList.remove("open", "l", "r"); openRow = null; return; }
      flushPending();
      row.style.transform = "translate3d(" + (dir * sw.offsetWidth) + "px,0,0)";
      sw.classList.add("gone");
      openRow = null;
      var parent = sw.parentNode, next = sw.nextSibling;
      setTimeout(function () { if (sw.parentNode) sw.parentNode.removeChild(sw); }, 260);
      PEND = { commit: job.commit, undo: function () {
        sw.classList.remove("gone", "open", "l", "r"); row.style.transform = "";
        parent.insertBefore(sw, next && next.parentNode === parent ? next : null);
        if (job.undo) job.undo();
      } };
      var mine = PEND;
      toast(job.label || "删掉了", { label: "撤销", fn: function () { if (PEND === mine) { PEND = null; mine.undo(); } } }, 4000);
      setTimeout(function () { if (PEND === mine) flushPending(); }, 4000);
    }
  }

  // ══ 二级页：历史记录 / 收藏夹 / 单个收藏夹 / 稍后再看 / 追番 / 本地片 ══
  function histRender() {
    var pg = $("pg-sub"), H = { cur: null, items: [], demo: false };
    pg.innerHTML = '<div class="blk flush"><div class="sk" style="height:90px;margin:8px 16px"></div></div>';
    var draw = function (canMore) {
      var groups = [], idx = {};
      H.items.forEach(function (it, i) {
        var w = String(it.view_at || ""), m = w.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})/);
        var g = m ? niceDay(m[1]) : /^今天/.test(w) ? "今天" : /^昨天/.test(w) ? "昨天" : "";
        if (g !== "今天" && g !== "昨天") g = "更早";
        if (!(g in idx)) { idx[g] = groups.length; groups.push({ g: g, rows: [] }); }
        var when = m ? (g === "更早" ? m[1].slice(5) + " " : "") + m[2] : w.replace(/^(今天|昨天)\s*/, "");
        groups[idx[g]].rows.push(rowHTML(it, i, [esc(it.pgc ? (it.episode || "番剧") : (it.up || "")), esc(when)], String(it.progress || "").replace(/^0:00\//, "")));
      });
      pg.innerHTML = (H.demo ? '<div class="demo-row" style="padding-top:8px">' + demoTag({ demo: true }) + "</div>" : "") +
        (groups.length ? groups.map(function (x, k) {
          return '<div class="blk flush stagger-in" style="--i:' + Math.min(k, 14) + (k ? ';margin-top:8px' : "") + '"><div class="grp">' + x.g + '</div><div class="rows">' + x.rows.join("") + "</div></div>";
        }).join("") : '<div class="empty-line">没有记录</div>') + (canMore ? moreBtn() : "");
      bindPlay(pg, H.items);
      var mb = pg.querySelector("[data-more]"); if (mb) mb.onclick = load;
    };
    var load = function () {
      var extra = H.cur ? { max: H.cur.max, view_at: H.cur.view_at, business: H.cur.business } : null;
      return get("history", null, extra).then(function (r) {
        H.demo = !!r.demo;
        H.items = H.items.concat(((r.data && r.data.list) || []).filter(function (x) { return x.bvid; }));
        var c = (r.data && r.data.cursor) || {};
        H.cur = c.max ? c : null;
        draw(!!(H.cur && r.data && r.data.has_more && !r.demo));
      });
    };
    return load();
  }
  function favRender() {
    var pg = $("pg-sub");
    pg.innerHTML = '<div class="card"><div class="sk" style="height:120px"></div></div>';
    return pocket().then(function (pk) {
      var fs = pk.mine, r = pk;   // AI 的收藏夹不在这里重复列出，它在 AI 的主页上
      if (!fs.length) { pg.innerHTML = '<div class="empty-line">没有收藏夹</div>'; return; }
      pg.innerHTML = (r.demo ? '<div class="demo-row">' + demoTag(r) + "</div>" : "") + fs.map(function (f, i) {
        return '<div class="card stagger-in" data-fid="' + esc(f.id) + '"' + sg(i) + '><div class="blk-hd"><span class="t">' + esc(f.title) + '</span><button class="more" type="button" data-open="' + esc(f.id) + '">' + esc(f.count) + " 个内容" + I("right", 13, 1.8) + '</button></div><div class="row3"></div></div>';
      }).join("");
      pg.querySelectorAll("[data-open]").forEach(function (b) { b.onclick = function () { parentOf.folder = "fav"; go("folder/" + b.dataset.open); }; });
      var fill = function (card) {
        if (card.dataset.done) return; card.dataset.done = "1";
        var box = card.querySelector(".row3");
        box.innerHTML = '<div class="sk" style="aspect-ratio:16/10"></div><div class="sk" style="aspect-ratio:16/10"></div><div class="sk" style="aspect-ratio:16/10"></div>';
        get("favpeek", { id: card.dataset.fid }).then(function (x) {
          var list = ((x.data && x.data.list) || []).slice(0, 3);
          box.innerHTML = list.map(function (it, i) { return '<div class="it" data-i="' + i + '" role="button" tabindex="0">' + cvHTML(it, "", "", true) + '<div class="t">' + esc(it.title) + "</div></div>"; }).join("");
          bindPlay(box, list);
        });
      };
      var cards = pg.querySelectorAll(".card[data-fid]");
      if ("IntersectionObserver" in window) {
        var io = new IntersectionObserver(function (es) { es.forEach(function (e) { if (e.isIntersecting) { io.unobserve(e.target); fill(e.target); } }); }, { root: $("body"), rootMargin: "300px 0px" });
        cards.forEach(function (c) { io.observe(c); });
      } else cards.forEach(fill);
    });
  }
  function folderRender(id) {
    var pg = $("pg-sub"), st = { page: 1, items: [] };
    pg.innerHTML = '<div id="fdGrid">' + skeleton(4) + "</div>";
    var load = function (more) {
      return get("favlist", { page: st.page, id: id }).then(function (r) {
        var list = (r.data && r.data.list) || [];
        if (r.data && r.data.title) $("topTitle").textContent = r.data.title;
        var start = st.items.length; st.items = st.items.concat(list);
        var box = $("fdGrid"); if (!box) return;
        if (!more) box.innerHTML = '<div class="grid2">' + list.map(function (it, i) { return cardHTML(it, i); }).join("") + "</div>";
        else box.querySelector(".grid2").insertAdjacentHTML("beforeend", list.map(function (it, k) { return cardHTML(it, start + k, k); }).join(""));
        var mr = box.querySelector(".more-row"); if (mr) mr.remove();
        if (r.data && r.data.has_more && list.length && !DEMO) { box.insertAdjacentHTML("beforeend", moreBtn()); box.querySelector("[data-more]").onclick = function () { st.page++; load(true); }; }
        if (!st.items.length) box.innerHTML = '<div class="empty-line">是空的</div>';
        bindPlay(box, st.items);
      });
    };
    return load(false);
  }
  function laterRender() {
    var pg = $("pg-sub");
    return get("watchlater", { offset: 0 }).then(function (r) {
      var list = (r.data && r.data.list) || [];
      pg.innerHTML = (r.demo ? '<div class="demo-row" style="padding-top:8px">' + demoTag(r) + "</div>" : "") +
        (list.length ? '<div class="blk flush" style="padding-top:8px"><div class="rows">' + list.map(function (it, i) { return swRowHTML(it, i, [esc(it.up || "")]); }).join("") + "</div></div>" : '<div class="empty-line">是空的</div>');
      bindPlay(pg, list);
      swipeList(pg, function (k) {
        var it = list[k]; if (!it || !it.bvid) return null;
        return { label: "从稍后再看里拿掉了", commit: function () {
          if (DEMO || r.demo) return;   // 示例数据不真删
          post("laterDel", { id: it.bvid, remove: true }).catch(function (e) { toast("没拿掉：" + e.message); });
        } };
      });
    });
  }
  // 追番 / 追剧（用户 B 站账号里的，只读）：顶上两个标签切换，一行一部，点击进入上次看到的那一集
  var PG = { type: "1" };
  function pgcRender() {
    var pg = $("pg-sub"), st = { page: 1, items: [] };
    var tabs = [["1", "追番"], ["2", "追剧"]].map(function (x) {
      return '<button type="button" data-t="' + x[0] + '" class="tab' + (PG.type === x[0] ? " on" : "") + '">' + x[1] + "</button>";
    }).join("");
    pg.innerHTML = '<div class="tabs">' + tabs + '</div><div id="pgcList"><div class="blk flush"><div class="sk" style="height:90px;margin:8px 16px"></div></div></div>';
    pg.querySelectorAll("[data-t]").forEach(function (b) { b.onclick = function () { if (PG.type !== b.dataset.t) { PG.type = b.dataset.t; pgcRender(); } }; });
    var load = function (more) {
      var want = PG.type;
      return get("pgcfollow", { type: want, page: st.page }, null, { noFake: true }).then(function (r) {
        var box = $("pgcList"); if (!box || PG.type !== want) return;
        if (r.err) { box.innerHTML = '<div class="empty-line">没拿到' + (/登录/.test(r.err.message || "") ? "（B 站没登录）" : "，往下拉一下再试") + "</div>"; return; }
        var list = (r.data && r.data.list) || [], start = st.items.length;
        st.items = st.items.concat(list);
        if (!st.items.length) { box.innerHTML = '<div class="empty-line">' + (want === "2" ? "还没追剧" : "还没追番") + "</div>"; return; }
        var rows = list.map(function (it, k) { return rowHTML(it, start + k, pgcLines(it), ""); }).join("");
        if (!more) box.innerHTML = '<div class="blk flush stagger-in" style="padding-top:8px"><div class="rows">' + rows + "</div></div>";
        else box.querySelector(".rows").insertAdjacentHTML("beforeend", rows);
        var mr = box.querySelector(".more-row"); if (mr) mr.remove();
        if (r.data && r.data.has_more) { box.insertAdjacentHTML("beforeend", moreBtn()); box.querySelector("[data-more]").onclick = function () { st.page++; load(true); }; }
        bindPlay(box, st.items);
      });
    };
    return load(false);
  }
  function filmsRender() {
    var pg = $("pg-sub");
    return get("films").then(function (r) {
      // 只列服务器上的本地片（B 站片子没有另一份「播放列表」，待看的单子是稍后再看）
      var list = ((r.data && r.data.films) || []).filter(function (f) { return f.kind !== "bili"; }).slice().reverse();
      pg.innerHTML = (r.demo ? '<div class="demo-row" style="padding-top:8px">' + demoTag(r) + "</div>" : "") +
        (list.length ? '<div class="blk flush" style="padding-top:8px"><div class="rows">' + list.map(function (f, i) {
          return swRowHTML(f, i, [esc(f.kind === "bili" ? (f.up || "") : "本地片")]);
        }).join("") + "</div></div>" : '<div class="empty-line">是空的</div>');
      bindPlay(pg, list);
      swipeList(pg, function (k) {
        var f = list[k]; if (!f) return null;
        // 本地片删除会连服务器上的视频文件一起删掉（后端 /remove 的行为），所以先确认一次；B 站视频只是从列表里移除
        if (f.kind !== "bili" && !window.confirm("「" + f.title + "」是服务器上的本地片，删了文件也一起没了。删吗？")) return null;
        return { label: "删了", commit: function () {
          if (DEMO || r.demo) return;
          post("remove", { id: f.id }).then(function () { TK = null; }).catch(function (e) { toast("没删掉：" + e.message); });
        } };
      });
    });
  }

  // ══ 皮肤：黑白（默认）和自定义两套，共用一个日夜开关。背景填充 / 背景模糊 / 主题色照 eryu 的做法，
  //    存在 localStorage（键前缀 config.storePrefix）。读到不认识的皮肤名一律当黑白。 ══
  var SKINS = { bw: "黑白", custom: "自定义" };
  function getSkin() { var v = document.documentElement.getAttribute("data-skin") || store.get("skin"); return v === "custom" ? "custom" : "bw"; }
  function applySkin(s, nosave) {
    if (!SKINS[s]) s = "bw";
    document.documentElement.setAttribute("data-skin", s);
    if (!nosave) store.set("skin", s);
    syncThemeColor();
    updateSkinBg();
    document.querySelectorAll(".skin-sw").forEach(function (b) { b.classList.toggle("active", b.dataset.skin === s); });
    document.documentElement.style.setProperty("--bg-show", String(getBgShow()));
    document.documentElement.style.setProperty("--bg-blur", getBgBlur() + "px");
    setAccent(getAccent());
  }
  // 自定义皮的底图：用户自己传的（存服务器 {api}/skin/bg）。没传就不垫图，看起来和黑白皮一样。
  // 有图时 <html> 加 data-bg="own"：区块变透明，托字的行和卡垫一层淡底，规则在 app.css 末尾。
  function ownBg() { return PR.skinBg || ""; }
  function updateSkinBg() {
    var el = $("skin-bg"), html = document.documentElement, own = getSkin() === "custom" ? ownBg() : "";
    el.style.backgroundImage = own ? 'url("' + own + '")' : "";
    if (own) html.setAttribute("data-bg", "own"); else html.removeAttribute("data-bg");
  }
  // 传图：先在浏览器里缩到长边 1600、转成质量 0.88 的 jpg，再整块 POST；反向代理限 256KB，超了再降一档
  function uploadSkinBg(file) {
    if (!file || !PF) return;
    toast("图在缩…");
    shrink(file, 1600, 0.88, 245 * 1024).then(function (b) { return PF.upload("skinbg", b); }).then(function (p) {
      PR = p; applySkin("custom"); toast("换好了");
      var v = $("skinbg-val"); if (v) v.textContent = "用的是你传的图";
    }).catch(function (e) { toast("没传上：" + upErr(e)); });
  }
  function clearSkinBg() {
    if (!PF) return;
    PF.upload("skinbg", null).then(function (p) { PR = p; }).catch(function () {}).then(function () {
      updateSkinBg();
      var v = $("skinbg-val"); if (v) v.textContent = "没传图";
      toast("去掉了");
    });
  }
  function getBgShow() { var v = parseFloat(store.get("bgshow_" + getSkin())); return isNaN(v) ? (C.bgShowDefault != null ? C.bgShowDefault : 1) : Math.min(1, Math.max(0, v)); }
  function setBgShow(v) { v = Math.min(1, Math.max(0, +v || 0)); store.set("bgshow_" + getSkin(), String(v)); document.documentElement.style.setProperty("--bg-show", String(v)); }
  function getBgBlur() { var v = parseInt(store.get("bgblur_" + getSkin()), 10); return isNaN(v) ? 14 : Math.min(40, Math.max(0, v)); }
  function setBgBlur(v) { v = Math.min(40, Math.max(0, parseInt(v, 10) || 0)); store.set("bgblur_" + getSkin(), String(v)); document.documentElement.style.setProperty("--bg-blur", v + "px"); }
  // 主题色只写 --key / --key-ink：选中的标签、底栏当前页、主要按钮、时间钮、链接用它；正文和面板不跟
  function defaultAccentHex() { return getTheme() === "light" ? "#111111" : "#f2f2f2"; }
  function hexToRgb(h) { h = h.replace("#", ""); if (h.length === 3) h = h.split("").map(function (c) { return c + c; }).join(""); var n = parseInt(h, 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; }
  function getAccent() { return store.get("accent_" + getSkin()) || ""; }
  function setAccent(hex) {
    var st = document.documentElement.style;
    store.set("accent_" + getSkin(), hex || "");
    if (!hex) { st.removeProperty("--key"); st.removeProperty("--key-ink"); return; }
    var c = hexToRgb(hex), lum = (0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]) / 255;
    st.setProperty("--key", "rgb(" + c + ")");
    st.setProperty("--key-ink", lum > 0.62 ? "#111111" : "#ffffff");
  }
  // ── 日夜：两套皮共用一个开关。用户点过就记到 <storePrefix>theme；没点过沿用 index.html <head> 里定的（config.followThemeKey 或系统） ──
  function getTheme() { return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light"; }
  function setTheme(t) {
    document.documentElement.setAttribute("data-theme", t === "dark" ? "dark" : "light");
    store.set("theme", t === "dark" ? "dark" : "light");
    syncThemeColor();
  }
  function syncThemeColor() {
    var tc = document.querySelector('meta[name="theme-color"]');
    if (tc) tc.content = getTheme() === "dark" ? "#0a0a0a" : "#ffffff";
  }

  // ══ 设置卡（结构照 eryu 的设置面板）：主题、日夜、背景三行、主题色、B 站登录、两个人、示例数据。
  //    背景三行（底图、填充、模糊）只在自定义皮下显示。 ══
  function openSettings() {
    var old = $("settings-sheet"); if (old) old.remove();
    var cur = getSkin(), th = getTheme();
    var sw = Object.keys(SKINS).map(function (k) {
      return '<button type="button" class="skin-sw' + (cur === k ? " active" : "") + '" data-skin="' + k + '" title="' + SKINS[k] + '" aria-label="' + SKINS[k] + '"></button>';
    }).join("");
    var raw = PR.raw || {};
    var el = document.createElement("div"); el.id = "settings-sheet"; el.className = "qr-sheet";
    el.innerHTML = '<div class="qr-card set-card"><div class="qr-title">设置</div><div class="home-settings">' +
      '<div class="hs-row"><span class="hs-label">主题</span><div class="skin-swatches">' + sw + "</div></div>" +
      '<div class="hs-row"><span class="hs-label">日夜</span><div class="hs-seg">' +
        '<button class="hs-btn' + (th === "light" ? " on" : " hs-btn-quiet") + '" type="button" data-th="light">' + I("sun", 14, 1.8) + '白天</button>' +
        '<button class="hs-btn' + (th === "dark" ? " on" : " hs-btn-quiet") + '" type="button" data-th="dark">' + I("moon", 14, 1.8) + "夜里</button></div></div>" +
      '<div class="hs-row" data-only="custom"><span class="hs-label">背景</span><span class="hs-val" id="skinbg-val">' + (ownBg() ? "用的是你传的图" : "没传图") + '</span><button class="hs-btn" id="skinbg-pick" type="button">选择</button><button class="hs-btn hs-btn-quiet" id="skinbg-clear" type="button">清除</button><input type="file" id="skinbg-file" accept="image/*" hidden></div>' +
      '<div class="hs-row" data-only="custom"><span class="hs-label">背景填充</span><input type="range" class="hs-range" id="bgshow-range" min="0" max="100" value="' + Math.round(getBgShow() * 100) + '" aria-label="背景填充"><span class="hs-val" id="bgshow-val" style="flex:0 0 3ch;text-align:right">' + Math.round(getBgShow() * 100) + "</span></div>" +
      '<div class="hs-row" data-only="custom"><span class="hs-label">背景模糊</span><input type="range" class="hs-range" id="bgblur-range" min="0" max="40" value="' + getBgBlur() + '" aria-label="背景模糊"><span class="hs-val" id="bgblur-val" style="flex:0 0 3ch;text-align:right">' + getBgBlur() + "</span></div>" +
      '<div class="hs-row"><span class="hs-label">当前主题色</span><span class="hs-val" id="accent-val">' + (getAccent() ? "自定义" : "默认") + '</span><label class="hs-btn accent-pick" for="accent-input" style="padding:0;width:34px;height:34px;border-radius:50%;overflow:hidden;position:relative;background:' + esc(getAccent() || "") + '"><input type="color" id="accent-input" value="' + esc(getAccent() || defaultAccentHex()) + '" style="position:absolute;inset:-8px;width:200%;height:200%;border:0;padding:0;opacity:' + (getAccent() ? 0 : 1) + ';cursor:pointer"></label><button class="hs-btn hs-btn-quiet" id="accent-clear" type="button">默认</button></div>' +
      '<div class="hs-row"><span class="hs-label">B 站</span><span class="hs-val" id="bili-val">在问…</span><button class="hs-btn" id="bili-btn" type="button" hidden>扫码登录</button></div>' +
      '<div class="hs-sec">两个人</div>' +
      '<div class="hs-row"><span class="hs-label">你</span>' + avHTML("user", "hs-avatar", 17, "button") + '<input class="hs-input" id="set-user-name" placeholder="' + esc((ME && ME.uname) || "你的名字") + '" value="' + esc(raw.userName || "") + '" maxlength="24"></div>' +
      '<div class="hs-row"><span class="hs-label">AI</span>' + avHTML("companion", "hs-avatar", 17, "button") + '<input class="hs-input" id="set-ai-name" placeholder="' + esc((CP && CP.name) || "AI 的名字") + '" value="' + esc(raw.companionName || "") + '" maxlength="24"></div>' +
      '<div class="hs-row"><span class="hs-label">签名</span><input class="hs-input" id="set-ai-sig" placeholder="' + esc((CP && CP.bio) || "AI 的签名") + '" value="' + esc(raw.companionBio || "") + '" maxlength="60"></div>' +
      '<div class="hs-row"><span class="hs-label">示例数据</span><span class="hs-val">' + (DEMO ? "开着（地址里 demo=1）" : "关着，用真数据") + '</span><button class="hs-btn hs-btn-quiet" id="demo-btn" type="button">' + (DEMO ? "关掉" : "打开") + "</button></div>" +
      '</div><div class="qr-actions"><button type="button" class="primary" id="set-close">好了</button></div></div>';
    document.body.appendChild(el);
    paintPeople(el);
    var close = function () { el.remove(); };
    el.addEventListener("click", function (e) { if (e.target === el) close(); });
    $("set-close").onclick = close;
    var syncRows = function () {
      el.querySelectorAll("[data-only]").forEach(function (r) { r.hidden = r.dataset.only !== getSkin(); });
      var r = $("bgshow-range"); r.value = Math.round(getBgShow() * 100); $("bgshow-val").textContent = r.value;
      var bl = $("bgblur-range"); bl.value = getBgBlur(); $("bgblur-val").textContent = bl.value;
      var a = $("accent-input"); a.value = getAccent() || defaultAccentHex(); a.parentElement.style.background = getAccent() || ""; a.style.opacity = getAccent() ? 0 : 1;
      $("accent-val").textContent = getAccent() ? "自定义" : "默认";
    };
    syncRows();
    el.querySelectorAll(".skin-sw").forEach(function (b) { b.onclick = function () { applySkin(b.dataset.skin); syncRows(); }; });
    el.querySelectorAll("[data-th]").forEach(function (b) {
      b.onclick = function () {
        setTheme(b.dataset.th); setAccent(getAccent()); syncRows();
        el.querySelectorAll("[data-th]").forEach(function (x) { var on = x.dataset.th === b.dataset.th; x.classList.toggle("on", on); x.classList.toggle("hs-btn-quiet", !on); });
      };
    });
    $("bgshow-range").oninput = function () { setBgShow(this.value / 100); $("bgshow-val").textContent = this.value; };
    $("bgblur-range").oninput = function () { setBgBlur(this.value); $("bgblur-val").textContent = this.value; };
    $("accent-input").oninput = function () { setAccent(this.value); $("accent-val").textContent = "自定义"; this.parentElement.style.background = this.value; this.style.opacity = 0; };
    $("accent-clear").onclick = function () { setAccent(""); syncRows(); };
    $("skinbg-pick").onclick = function () { $("skinbg-file").click(); };
    $("skinbg-file").onchange = function () { uploadSkinBg(this.files && this.files[0]); this.value = ""; };
    $("skinbg-clear").onclick = clearSkinBg;
    $("demo-btn").onclick = function () { var q = new URLSearchParams(location.search); if (DEMO) q.delete("demo"); else q.set("demo", "1"); location.search = q.toString(); };
    // 两个人：改完离开输入框（change 事件）就存；清空＝恢复默认
    var save = function (id, key) {
      var inp = $(id);
      inp.addEventListener("change", function () {
        if (!PF) return;
        var patch = {}; patch[key] = inp.value.trim();
        PF.save(patch).then(onPrefs).catch(function (e) { toast("没存上：" + e.message); });
      });
    };
    save("set-user-name", "userName"); save("set-ai-name", "companionName"); save("set-ai-sig", "companionBio");
    el.querySelectorAll(".hs-avatar").forEach(function (b) { b.onclick = function () { changeAvatar(b.dataset.av); }; });
    refreshBiliRow();
  }
  function refreshBiliRow() {
    var v = $("bili-val"), b = $("bili-btn"); if (!v) return;
    loadMe().then(function (r) {
      if (!$("bili-val")) return;
      var d = ME || {};
      v.textContent = r.demo ? "示例账号" : d.logged_in ? "已登录 · " + (d.uname || "") : r.err ? "问不到" : "没登录";
      b.hidden = false;
      b.textContent = d.logged_in && !r.demo ? "账号" : "扫码登录";
      b.onclick = function () { if (d.logged_in && !r.demo) openAccount(); else openQrLogin(); };
    });
  }
  $("setBtn").addEventListener("click", function (e) {
    if (EMBED && $("setBtn").dataset.mode === "me") { go("me"); return; }   // 嵌入模式：推荐页右边那颗是「我的」
    openSettings(e);
  });
  if (EMBED) $("homeBtn").addEventListener("click", function (e) {          // 嵌入模式：返回按钮切页也不留历史记录
    var h = ($("homeBtn").getAttribute("href") || "").replace(/^#/, "");
    if (!h || /^[a-z]+:|^\//i.test(h)) return;
    e.preventDefault(); go(h);
  });

  // ══ B 站扫码登录：弹一张卡显示二维码，每 2 秒查一次进度。
  //    二维码由后端生成（POST {api}/bili/login/qr 返回一张 png），进度查 {api}/bili/login/state。 ══
  var qrPoll = null, qrSeq = 0;
  function closeQr() {
    clearInterval(qrPoll); qrPoll = null; qrSeq++;
    var el = $("qr-sheet"); if (el) el.classList.add("hidden");
  }
  function qrSheet() {
    var el = $("qr-sheet");
    if (!el) {
      el = document.createElement("div"); el.id = "qr-sheet"; el.className = "qr-sheet hidden";
      el.innerHTML = '<div class="qr-card"><div class="qr-title" id="qr-title"></div><div id="qr-body"></div><div class="qr-msg" id="qr-msg"></div><div class="qr-actions" id="qr-actions"></div></div>';
      document.body.appendChild(el);
      el.addEventListener("click", function (e) { if (e.target === el) closeQr(); });
    }
    el.classList.remove("hidden");
    var card = el.querySelector(".qr-card"); card.style.animation = "none"; void card.offsetWidth; card.style.animation = "";
    return el;
  }
  function qrActions(btns) {
    var box = $("qr-actions");
    box.innerHTML = btns.map(function (b, i) { return '<button type="button"' + (b.primary ? ' class="primary"' : "") + ' data-k="' + i + '">' + esc(b.label) + "</button>"; }).join("");
    box.querySelectorAll("[data-k]").forEach(function (x) { x.onclick = btns[+x.dataset.k].fn; });
  }
  // 已登录：显示账号，可以退出或换号重新扫码
  function openAccount() {
    qrSheet();
    clearInterval(qrPoll); qrPoll = null;
    $("qr-title").textContent = "B 站 · 账号";
    $("qr-body").innerHTML = '<div class="acct">' + avHTML("user", "acct-av", 26) + '<div class="acct-name">' + esc((ME && ME.uname) || "") + "</div></div>";
    paintPeople($("qr-body"));
    $("qr-msg").textContent = "登录着";
    qrActions([
      { label: "先不了", fn: closeQr },
      { label: "退出", fn: confirmLogout },
      { label: "重新扫码", primary: true, fn: function () { openQrLogin(); } }
    ]);
  }
  // 退出：同一张卡上先问一句，点了「退出登录」才发
  function confirmLogout() {
    $("qr-msg").textContent = "退出 B 站登录？推荐、收藏、稍后再看都会看不到，要重新扫码才回来";
    qrActions([{ label: "先不了", fn: closeQr }, { label: "退出登录", primary: true, fn: doLogout }]);
  }
  // 服务器上的凭据一定清掉；B 站那头作废是尽力而为，没确认就照实说（remote 不是 ok）
  function doLogout() {
    var msg = $("qr-msg");
    msg.textContent = "在退…"; qrActions([]);
    post("logout").then(function (d) {
      afterLogout();
      $("qr-body").innerHTML = "";
      if (d.remote === "ok") { msg.textContent = "退出了"; setTimeout(closeQr, 900); return; }
      msg.textContent = "服务器上的登录已经清掉了；B 站那头没确认这次登录失效" + (d.note ? "（" + d.note + "）" : "");
      qrActions([{ label: "知道了", primary: true, fn: closeQr }]);
    }).catch(function (e) {
      msg.textContent = (e && e.message) || "没退成，待会再试";
      qrActions([{ label: "先不了", fn: closeQr }, { label: "再试一次", primary: true, fn: doLogout }]);
    });
  }
  // 退出以后上一个号的东西一样不留：账号信息、收藏夹、推荐那一批（连 sessionStorage 里那份）、关注页；各页下次进来重新拉，眼前这页当场重画
  function afterLogout() {
    meP = null; ME = null; PK = null; lcDrop();
    R.items = []; F.host = null; F.hostName = null; F.ups = []; F.all = [];
    Object.keys(drawn).forEach(function (k) { delete drawn[k]; });
    refreshBiliRow();
    if (SUBS[curPage]) show(location.hash.slice(1), true);
    else if (curPage) { drawn[curPage] = 1; PAGES[curPage](); }
    loadMe().then(function () { paintPeople(); });
  }
  function openQrLogin() {
    qrSheet();
    $("qr-title").textContent = "B 站 · 扫码登录";
    $("qr-body").innerHTML = '<div class="qr-box" id="qr-box"></div>';
    qrActions([{ label: "先不了", fn: closeQr }, { label: "换一张", primary: true, fn: function () { startQr(); } }]);
    startQr();
  }
  function startQr() {
    clearInterval(qrPoll); qrPoll = null;
    var seq = ++qrSeq;
    var box = $("qr-box"), msg = $("qr-msg");
    box.innerHTML = '<div class="spinner"></div>'; msg.textContent = "找 B 站要二维码…";
    post("loginQr").then(function (d) {
      if (seq !== qrSeq) return;
      if (!d.ok || !d.img) throw new Error(d.error || "");
      box.innerHTML = '<img alt="B 站登录二维码" src="' + esc(d.img) + '">';
      msg.textContent = "打开 B 站 App，扫这个";
      qrPoll = setInterval(function () {
        get("loginState", null, null, { noFake: true }).then(function (r) {
          if (seq !== qrSeq) return;
          var s = r.data || {};
          if (s.paused) { clearInterval(qrPoll); qrPoll = null; msg.textContent = "B 站风控暂停中，先在服务器终端解除再扫"; }
          else if (s.progress === "expired" || s.progress === "timeout") { clearInterval(qrPoll); qrPoll = null; msg.textContent = "二维码过期了，点「换一张」"; }
          else if (s.progress === "error") { clearInterval(qrPoll); qrPoll = null; msg.textContent = "没登上，点「换一张」再试"; }
          else if (s.progress === "ok") {
            clearInterval(qrPoll); qrPoll = null;
            msg.textContent = "登录好了" + (s.uname ? " · " + s.uname : "");
            toast("B 站登录好了"); meP = null; ME = null; refreshBiliRow(); loadMe().then(function () { paintPeople(); });
            setTimeout(closeQr, 900);
          }
        });
      }, 2000);
    }).catch(function (e) {
      if (seq !== qrSeq) return;
      msg.textContent = "B 站那边没给二维码" + (e && e.message ? "：" + e.message : "，待会再试"); box.innerHTML = "";
    });
  }

  // ══ 下拉刷新：在页面最顶上往下拉，过 64px 松手就刷新。推荐＝换一批，其他页＝重新取数。
  //    只在确认是「在顶上往下拉」之后才 preventDefault，横滑、往上滑都不拦。 ══
  var PTR = { y0: null, x0: 0, on: false, busy: false, d: 0 };
  var ptrEl = $("ptr"), body = $("body");
  function curPageEl() { return document.querySelector(".page:not([hidden])"); }
  function ptrSet(d, anim) {
    var pg = curPageEl();
    [pg, ptrEl].forEach(function (x) { if (x) x.style.transition = anim ? "transform var(--t-base) var(--ease-out), opacity var(--t-fast) var(--ease-out)" : "none"; });
    if (pg) pg.style.transform = d ? "translate3d(0," + d + "px,0)" : "";
    ptrEl.style.transform = "translate3d(-50%," + (d - 44) + "px,0)";
    ptrEl.style.opacity = d ? Math.min(1, d / 64) : 0;
    ptrEl.querySelector(".ic").style.transform = "rotate(" + Math.round(d * 4) + "deg)";
  }
  function refreshCur() {
    var p = curPage;
    if (SUBS[p]) { if (p === "companion") TK = null; if (p === "companion" || p === "fav") PK = null; return show(location.hash.slice(1), true); }
    drawn[p] = 1;
    if (p === "rcmd") { if (R.mode === "rcmd") R.fresh++; R.page = 1; lcDrop(); return rcmdRender(); }
    if (p === "talked") TK = null;
    if (p === "me") { PK = null; TK = null; meP = null; }
    if (p === "follow") F.host = null;
    return PAGES[p]();
  }
  body.addEventListener("touchstart", function (e) {
    if (PTR.busy || e.touches.length !== 1 || body.scrollTop > 0 || $("settings-sheet")) { PTR.y0 = null; return; }
    PTR.y0 = e.touches[0].clientY; PTR.x0 = e.touches[0].clientX; PTR.on = false; PTR.d = 0;
  }, { passive: true });
  body.addEventListener("touchmove", function (e) {
    if (PTR.y0 == null) return;
    var dy = e.touches[0].clientY - PTR.y0, dx = e.touches[0].clientX - PTR.x0;
    if (!PTR.on) {
      if (dy > 8 && dy > Math.abs(dx) * 1.2 && body.scrollTop <= 0) PTR.on = true;
      else if (Math.abs(dx) > 8 || dy < -4) { PTR.y0 = null; return; }
      else return;
    }
    if (e.cancelable) e.preventDefault();
    PTR.d = Math.min(110, dy * 0.5);
    ptrSet(Math.max(0, PTR.d), false);
  }, { passive: false });
  var ptrEnd = function () {
    if (PTR.y0 == null) return;
    PTR.y0 = null;
    if (!PTR.on) return;
    PTR.on = false;
    if (PTR.d < 64) { ptrSet(0, true); return; }
    PTR.busy = true; ptrEl.classList.add("busy"); ptrSet(52, true);
    var t0 = Date.now();
    Promise.resolve().then(refreshCur).catch(function () {}).then(function () {
      setTimeout(function () { PTR.busy = false; ptrEl.classList.remove("busy"); ptrSet(0, true); }, Math.max(0, 500 - (Date.now() - t0)));
    });
  };
  body.addEventListener("touchend", ptrEnd);
  body.addEventListener("touchcancel", ptrEnd);

  // ══ 切页：四个主页签＋几张二级页（二级页都挂在「我的」底下，顶栏左边那颗变成返回）。页签记在地址的 #hash 里 ══
  var drawn = {};
  var PAGES = { rcmd: rcmdRender, follow: followRender, talked: talkedRender, me: meRender };
  var SUBS = { hist: [histRender, "历史记录"], fav: [favRender, "收藏夹"], folder: [folderRender, "收藏夹"], later: [laterRender, "稍后再看"], films: [filmsRender, "本地片"], pgc: [pgcRender, "追番"],
    companion: [companionRender, function () { return aiName() + "的主页"; }] };
  var parentOf = { folder: "fav" };   // 二级页的返回按钮回哪：默认回「我的」；单个收藏夹页回打开它的那页（收藏夹 / AI 的主页）
  var TITLES = { rcmd: "", follow: "关注", talked: "一起聊过的片", me: "" };
  var scrolls = {};
  var curPage = null;
  function go(p) {
    if (location.hash === "#" + p) return show(p);
    if (EMBED) location.replace("#" + p); else location.hash = p;   // 嵌入模式切页不留历史记录（hashchange 照样会触发）
  }
  // 切页入场：整页轻轻淡入（.tab-body.tab-enter，靠读一次 offsetWidth 重启动画），列表逐行入场交给 .stagger-in
  function enter() { body.classList.remove("tab-enter"); void body.offsetWidth; body.classList.add("tab-enter"); }
  function show(h, quiet) {
    var parts = String(h || "").split("/"), p = parts[0], arg = parts.slice(1).join("/");
    var sub = SUBS[p];
    if (!sub && !PAGES[p]) p = "rcmd";
    if (curPage && !SUBS[curPage]) scrolls[curPage] = body.scrollTop;
    flushPending();
    var changed = curPage !== p;
    curPage = p;
    document.body.dataset.page = p;
    var shown = sub ? "sub" : p;
    document.querySelectorAll(".page").forEach(function (s) { s.hidden = s.dataset.page !== shown; s.style.transform = ""; });
    document.querySelectorAll(".nav-item").forEach(function (b) { b.classList.toggle("active", b.dataset.go === (sub ? "me" : p)); });
    $("searchForm").hidden = p !== "rcmd";
    $("topTitle").textContent = sub ? (typeof sub[1] === "function" ? sub[1]() : sub[1]) : TITLES[p] || "";
    var hb = $("homeBtn");
    if (sub) { hb.href = "#" + (parentOf[p] || "me"); hb.hidden = false; hb.setAttribute("aria-label", "返回"); }
    else if (EMBED) {                    // 嵌入模式：没有底栏也没有「回主页」——推荐页左边不放钮；我的 → 回推荐；关注 / 聊过 → 回我的
      hb.href = "#" + (p === "me" ? "rcmd" : "me"); hb.hidden = p === "rcmd"; hb.setAttribute("aria-label", "返回");
    }
    else { hb.href = C.homeUrl || "#"; hb.hidden = !C.homeUrl; hb.setAttribute("aria-label", "回主页"); }
    if (EMBED) {                         // 右边那颗：推荐页＝进「我的」，「我的」＝设置，别的页不放
      var sb = $("setBtn"), mode = p === "rcmd" ? "me" : p === "me" ? "set" : "";
      sb.hidden = !mode;
      if (mode && sb.dataset.mode !== mode) {
        sb.dataset.mode = mode;
        sb.innerHTML = mode === "me" ? I("user", 19, 1.8) : I("gear", 19, 1.8);
        sb.setAttribute("aria-label", mode === "me" ? "我的" : "设置"); sb.title = mode === "me" ? "我的" : "设置";
      }
    }
    if (changed && !quiet) enter();
    if (sub) { if (!quiet) $("pg-sub").innerHTML = ""; var r = sub[0](arg); if (!quiet) body.scrollTop = 0; return r; }
    if (!drawn[p]) { drawn[p] = 1; PAGES[p](); }
    body.scrollTop = scrolls[p] || 0;
  }
  $("nav").addEventListener("click", function (e) {
    var b = e.target.closest(".nav-item"); if (!b) return;
    if (b.dataset.go === curPage) { body.scrollTo({ top: 0, behavior: "smooth" }); return; }
    go(b.dataset.go);
  });
  window.addEventListener("hashchange", function () { show(location.hash.slice(1)); });
  document.addEventListener("keydown", function (e) {
    if ((e.key === "Enter" || e.key === " ") && e.target.matches && e.target.matches('[role="button"]')) { e.preventDefault(); e.target.click(); }
  });

  applySkin(getSkin(), true);   // 开页只应用 <head> 里定好的那套，不写存储；在设置里点过才记
  show(location.hash.slice(1) || "rcmd");
  if (PF) PF.load().then(function (p) { PR = p; paintPeople(); if (getSkin() === "custom") updateSkinBg(); });
  loadMe().then(function () { paintPeople(); });

  // ══ 嵌入模式：和宿主页面之间的 postMessage（只认同源），消息名和播放页（client/index.html）是同一套 ══
  // 往外发：bilitube:now 报告「没在播放」；bilitube:size 报告想要的高度（这里报一个很大的值，由宿主自己限高）；
  //         bilitube:skin 报告当前几样颜色，宿主可以拿去给 iframe 外框上色，颜色变了会再报一次。
  // 往里收：bilitube:skin? 请求重报颜色；bilitube:cmd {cmd:"shelf"} 在稍后再看页和推荐页之间切换。
  if (EMBED) {
    var tellUp = function (m) { try { window.parent.postMessage(m, location.origin); } catch (e) {} };
    var skinTell = function () {
      var de = document.documentElement, cs = getComputedStyle(de), g = function (k) { return cs.getPropertyValue(k).trim(); };
      tellUp({ type: "bilitube:skin", from: "own", vars: {
        panel: g("--paper"), bg: g("--seam"), tile: g("--milk"), tile2: g("--milk-strong"),
        ink: g("--ink"), ink2: g("--ink-soft"), ink3: g("--muted"), accent: g("--btn"), accent2: g("--ink"), onAccent: g("--btn-ink"),
        line: g("--line"), radius: "14px", dark: de.getAttribute("data-theme") === "dark" } });
    };
    tellUp({ type: "bilitube:now", film: null, id: null, t: 0, playing: false });
    tellUp({ type: "bilitube:size", h: 4000 });
    skinTell();
    new MutationObserver(function () { requestAnimationFrame(skinTell); })
      .observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "data-skin", "data-bg", "style"] });
    window.addEventListener("message", function (ev) {
      if (ev.origin !== location.origin || ev.source !== window.parent) return;
      var d = ev.data || {};
      if (d.type === "bilitube:skin?") skinTell();
      else if (d.type === "bilitube:cmd" && d.cmd === "shelf") go(curPage === "later" ? "rcmd" : "later");   // 宿主头上那颗「稍后再看」
    });
  }
})();
