// HKBT 站点交互：导航、滚动进场、物流查询（演示）、表单提示
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

  var rvs = document.querySelectorAll('.rv');
  if ('IntersectionObserver' in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en, i) {
        if (en.isIntersecting) {
          setTimeout(function () { en.target.classList.add('in'); }, (i % 4) * 70);
          io.unobserve(en.target);
        }
      });
    }, { threshold: 0.12, rootMargin: '0px 0px -40px 0px' });
    rvs.forEach(function (el) { io.observe(el); });
  } else {
    rvs.forEach(function (el) { el.classList.add('in'); });
  }
})();

// ===== 物流查询（演示数据，同一单号结果稳定）=====
window.hkbtTrack = function () {
  var input = document.getElementById('trInput');
  var box = document.getElementById('trResult');
  if (!input || !box) return;
  var code = input.value.trim();
  if (!code) { box.innerHTML = '<p>请输入单号后再查询。</p>'; return; }

  var STEPS = [
    ['订单确认', '已收到订单，排产中'],
    ['生产完成', '批次检验合格，等待出货'],
    ['已出货', '装车离厂，运往起运仓'],
    ['运输中', '头程在途，预计 5 个工作日到港'],
    ['已签收', '目的地仓库签收完成']
  ];
  var h = 0;
  for (var i = 0; i < code.length; i++) h = (h * 31 + code.charCodeAt(i)) % 9973;
  var idx = 1 + (h % 4);
  var base = new Date(2026, 8, 18);
  var rows = '';
  for (var j = 0; j < STEPS.length; j++) {
    var done = j <= idx;
    var dt = new Date(base.getTime() + j * 3 * 86400000);
    var stamp = dt.getFullYear() + '-' + String(dt.getMonth() + 1).padStart(2, '0') +
      '-' + String(dt.getDate()).padStart(2, '0');
    rows += '<div style="display:flex;gap:14px;padding:8px 0;' + (done ? '' : 'opacity:.35') + '">' +
      '<span style="flex:0 0 88px;color:var(--muted);font-size:12.5px">' + (done ? stamp : '—') + '</span>' +
      '<span style="flex:0 0 92px;font-weight:600;color:' + (done ? 'var(--ink)' : 'var(--muted)') + '">' +
      STEPS[j][0] + '</span>' +
      '<span style="color:var(--muted);font-size:13.5px">' + STEPS[j][1] + '</span></div>';
  }
  box.innerHTML =
    '<div style="font-size:11.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)">单号</div>' +
    '<div style="font-family:var(--font-h);font-size:20px;margin:4px 0 10px">' +
    code.replace(/[<>&"]/g, '') + '</div>' +
    '<div style="margin-bottom:14px">当前状态：<b>' + STEPS[idx][0] + '</b></div>' + rows +
    '<p style="margin-top:14px;font-size:12px;color:var(--muted)">以上为示例数据，正式部署后请接入真实物流接口。</p>';
};

// ===== 联系表单（演示，不提交）=====
window.hkbtForm = function (e) {
  if (e && e.preventDefault) e.preventDefault();
  var f = e.target || document.querySelector('.contact-form');
  var name = (f.querySelector('[name=name]') || {}).value || '';
  var btn = f.querySelector('button[type=submit]');
  var old = btn ? btn.textContent : '';
  if (btn) {
    btn.textContent = name ? '已收到，' + name + '，我们会尽快联系' : '已收到，我们会尽快联系';
    btn.disabled = true;
    setTimeout(function () { btn.textContent = old; btn.disabled = false; }, 3200);
  }
  return false;
};
