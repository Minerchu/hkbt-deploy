// HKBT 官网交互
(function () {
  var nav = document.getElementById('nav');
  var toggle = document.getElementById('navtoggle');
  var links = document.getElementById('navlinks');

  window.addEventListener('scroll', function () {
    if (nav) nav.classList.toggle('solid', window.scrollY > 40);
  }, { passive: true });

  if (toggle && links) {
    toggle.addEventListener('click', function () {
      var open = links.classList.toggle('open');
      toggle.textContent = open ? '✕' : '☰';
    });
    links.addEventListener('click', function (e) {
      if (e.target.tagName === 'A') {
        links.classList.remove('open');
        toggle.textContent = '☰';
      }
    });
  }

  // 滚动进场
  var rvs = document.querySelectorAll('.rv');
  if ('IntersectionObserver' in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en, i) {
        if (en.isIntersecting) {
          var d = (i % 4) * 70;
          setTimeout(function () { en.target.classList.add('in'); }, d);
          io.unobserve(en.target);
        }
      });
    }, { threshold: 0.12, rootMargin: '0px 0px -40px 0px' });
    rvs.forEach(function (el) { io.observe(el); });
  } else {
    rvs.forEach(function (el) { el.classList.add('in'); });
  }

  // 数字滚动
  var counters = document.querySelectorAll('[data-cnt]');
  function run(el) {
    var target = parseFloat(el.getAttribute('data-cnt'));
    var dec = parseInt(el.getAttribute('data-dec') || '0', 10);
    var start = performance.now();
    var dur = 1500;
    function step(now) {
      var p = Math.min((now - start) / dur, 1);
      var eased = 1 - Math.pow(1 - p, 3);
      var v = target * eased;
      el.textContent = dec > 0 ? v.toFixed(dec) : Math.round(v);
      if (p < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }
  if ('IntersectionObserver' in window) {
    var co = new IntersectionObserver(function (es) {
      es.forEach(function (e) {
        if (e.isIntersecting) { run(e.target); co.unobserve(e.target); }
      });
    }, { threshold: 0.5 });
    counters.forEach(function (el) { co.observe(el); });
  } else {
    counters.forEach(run);
  }

  // 进度条
  var bars = document.querySelectorAll('.bar i');
  if ('IntersectionObserver' in window) {
    var bo = new IntersectionObserver(function (es) {
      es.forEach(function (e) {
        if (e.isIntersecting) {
          e.target.style.width = e.target.getAttribute('data-w') + '%';
          bo.unobserve(e.target);
        }
      });
    }, { threshold: 0.4 });
    bars.forEach(function (el) { bo.observe(el); });
  } else {
    bars.forEach(function (el) { el.style.width = el.getAttribute('data-w') + '%'; });
  }
})();

// ===== 物流查询（演示数据）=====
(function () {
  var btn = document.getElementById('trackBtn');
  var input = document.getElementById('trackInput');
  var box = document.getElementById('trackResult');
  if (!btn || !input || !box) return;

  var STEPS = [
    ['01 已下单', '订单确认，供应商排产'],
    ['02 已出货', '工厂装车，送往起运仓'],
    ['03 已报关', '出口申报放行，单证齐全'],
    ['04 已开船', '头程运输中，预计 32 天后到港'],
    ['05 已到港', '目的港清关，等待放行'],
    ['06 已入仓', '海外仓上架，库存可用'],
    ['07 已派送', '尾程交付完成']
  ];

  function render(code) {
    if (!code) {
      box.style.display = 'block';
      box.innerHTML = '<p>请输入运单号后再查询。</p>';
      return;
    }
    // 用单号做稳定伪随机，保证同一单号每次结果一致
    var h = 0;
    for (var i = 0; i < code.length; i++) h = (h * 31 + code.charCodeAt(i)) % 9973;
    var idx = 2 + (h % 4);
    var d = new Date(2026, 8, 18);
    var rows = '';
    for (var j = 0; j < STEPS.length; j++) {
      var done = j <= idx;
      var dt = new Date(d.getTime() + j * 4 * 86400000);
      var stamp = dt.getFullYear() + '-' + String(dt.getMonth() + 1).padStart(2, '0') + '-' + String(dt.getDate()).padStart(2, '0');
      rows += '<div style="display:flex;gap:16px;padding:9px 0;' + (done ? '' : 'opacity:.38') + '">' +
        '<span style="flex:0 0 92px;color:#7A6F63;font-size:13px">' + (done ? stamp : '—') + '</span>' +
        '<span style="flex:0 0 96px;font-weight:600;color:' + (done ? '#191512' : '#7A6F63') + '">' + STEPS[j][0] + '</span>' +
        '<span style="color:#7A6F63;font-size:14px">' + STEPS[j][1] + '</span></div>';
    }
    box.style.display = 'block';
    box.innerHTML =
      '<div style="font-size:13px;letter-spacing:.1em;text-transform:uppercase;color:#7A6F63;margin-bottom:6px">运单号</div>' +
      '<div style="font-family:var(--serif);font-size:22px;margin-bottom:8px">' + code.replace(/[<>&]/g, '') + '</div>' +
      '<div style="margin-bottom:18px">当前状态：<span class="st">' + STEPS[idx][0] + '</span></div>' +
      rows +
      '<p style="margin-top:18px;font-size:12.5px;color:#7A6F63">以上为示例数据，未接入真实承运商接口。</p>';
  }

  btn.addEventListener('click', function () { render(input.value.trim()); });
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); render(input.value.trim()); }
  });
  render(input.value.trim());
})();

// ===== 联系表单 =====
(function () {
  var f = document.getElementById('hkbtForm');
  if (!f) return;
  f.addEventListener('submit', function () {
    var tip = document.getElementById('formTip');
    if (!tip) return;
    var name = (f.querySelector('[name=name]') || {}).value || '';
    tip.textContent = name
      ? '已收到「' + name + '」的需求信息。本站为演示环境，表单未接入后端，请直接通过页面右侧邮箱与我们联系。'
      : '已收到需求信息。本站为演示环境，表单未接入后端，请直接通过页面右侧邮箱与我们联系。';
    tip.style.color = '#D5202A';
  });
})();
