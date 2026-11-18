/* 图标。全部是 24×24 网格、currentColor 描边、圆头圆角，不填色。
   线宽由调用处给：底栏 1.5（当前页 2.1，见 app.css .nav-item svg），顶栏圆钮 1.8，搜索框 2。

   来源两家，每一枚都和原文逐字核对过：
   · Feather Icons（https://feathericons.com ，MIT，Copyright (c) 2013-2017 Cole Bemis），经 eryu 转手：
       home  ＝ Feather 4.29.2 的 home
       x     ＝ Feather 4.29.2 的 x
   · Lucide（https://lucide.dev ，ISC License，Copyright (c) Lucide Contributors 2022；
     portions Copyright (c) Cole Bemis 2013-2022 (Feather, MIT)），取自 lucide-static 0.460.0，键名 → Lucide 名：
       back → chevron-left      gear → settings          cloud → cloud          search → search
       users → users            chat → message-circle    refresh → refresh-cw   right → chevron-right
       down → chevron-down      radio → radio            play → play            link → link
       tv → tv                  sun → sun                moon → moon            clock → clock
       dm → message-square-text star → star            history → history      clapper → clapperboard
       list → list-video        trash → trash-2          user → user-round      arrow → arrow-right
   许可原文见仓库根目录 NOTICE。 */
window.BILITUBE_ICONS = (function () {
  var P = {
    /* Feather（经 eryu） */
    home:   '<path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/>',
    x:      '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
    /* Lucide */
    back:   '<path d="m15 18-6-6 6-6"/>',
    gear:   '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>',
    cloud:  '<path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z"/>',
    search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    chat:   '<path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z"/>',
    refresh:'<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
    right:  '<path d="m9 18 6-6-6-6"/>',
    down:   '<path d="m6 9 6 6 6-6"/>',
    radio:  '<path d="M4.9 19.1C1 15.2 1 8.8 4.9 4.9"/><path d="M7.8 16.2c-2.3-2.3-2.3-6.1 0-8.5"/><circle cx="12" cy="12" r="2"/><path d="M16.2 7.8c2.3 2.3 2.3 6.1 0 8.5"/><path d="M19.1 4.9C23 8.8 23 15.1 19.1 19"/>',
    play:   '<polygon points="6 3 20 12 6 21 6 3"/>',
    link:   '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
    tv:     '<rect width="20" height="15" x="2" y="7" rx="2" ry="2"/><polyline points="17 2 12 7 7 2"/>',
    sun:    '<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/>',
    moon:   '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
    clock:  '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    dm:     '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/><path d="M13 8H7"/><path d="M17 12H7"/>',
    star:   '<path d="M11.525 2.295a.53.53 0 0 1 .95 0l2.31 4.679a2.123 2.123 0 0 0 1.595 1.16l5.166.756a.53.53 0 0 1 .294.904l-3.736 3.638a2.123 2.123 0 0 0-.611 1.878l.882 5.14a.53.53 0 0 1-.771.56l-4.618-2.428a2.122 2.122 0 0 0-1.973 0L6.396 21.01a.53.53 0 0 1-.77-.56l.881-5.139a2.122 2.122 0 0 0-.611-1.879L2.16 9.795a.53.53 0 0 1 .294-.906l5.165-.755a2.122 2.122 0 0 0 1.597-1.16z"/>',
    history:'<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/>',
    clapper:'<path d="M20.2 6 3 11l-.9-2.4c-.3-1.1.3-2.2 1.3-2.5l13.5-4c1.1-.3 2.2.3 2.5 1.3Z"/><path d="m6.2 5.3 3.1 3.9"/><path d="m12.4 3.4 3.1 4"/><path d="M3 11h18v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/>',
    /* list＝稍后再看那一段，trash＝横滑删除，user＝没有头像时的占位，arrow＝直播房间号的提交钮 */
    list:   '<path d="M12 12H3"/><path d="M16 6H3"/><path d="M12 18H3"/><path d="m16 12 5 3-5 3v-6Z"/>',
    trash:  '<path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/><line x1="10" x2="10" y1="11" y2="17"/><line x1="14" x2="14" y1="11" y2="17"/>',
    user:   '<circle cx="12" cy="8" r="5"/><path d="M20 21a8 8 0 0 0-16 0"/>',
    arrow:  '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>'
  };
  /* ic('home', 20, 1.5) → 一整枚 <svg> 字符串；不给 sw 就由 CSS 定线宽（底栏靠 .nav-item svg 的 1.5 / 2.1） */
  function ic(name, size, sw) {
    size = size || 20;
    return '<svg class="ic" viewBox="0 0 24 24" width="' + size + '" height="' + size + '"' +
      (sw ? ' stroke-width="' + sw + '"' : "") + ' aria-hidden="true">' + (P[name] || "") + "</svg>";
  }
  /* 页面里写 <i data-ic="名字" data-s="尺寸" data-w="线宽"></i>，mount() 把它们换成 <svg> */
  function mount(root) {
    (root || document).querySelectorAll("i[data-ic]").forEach(function (el) {
      el.outerHTML = ic(el.dataset.ic, +el.dataset.s || 20, el.dataset.w || null);
    });
  }
  return { ic: ic, mount: mount, paths: P };
})();
