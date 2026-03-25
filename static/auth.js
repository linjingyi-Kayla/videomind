/**
 * JWT：localStorage key `vm_access_token`，与登录页 /api/login 返回一致。
 */
function vmGetToken() {
  return localStorage.getItem('vm_access_token');
}

function vmSetToken(token) {
  if (token) localStorage.setItem('vm_access_token', token);
  else localStorage.removeItem('vm_access_token');
}

function vmGetUserEmail() {
  return localStorage.getItem('vm_user_email') || '';
}

function vmSetUserEmail(email) {
  if (email) localStorage.setItem('vm_user_email', email);
  else localStorage.removeItem('vm_user_email');
}

/** 退出登录：清除 Token 与本地缓存的邮箱 */
function vmLogout() {
  try {
    fetch('/api/logout', { method: 'POST', credentials: 'include', cache: 'no-store' }).catch(function () {});
  } catch (e) {}
  vmSetToken(null);
  vmSetUserEmail(null);
}

function vmAuthHeaders() {
  const h = { 'Cache-Control': 'no-cache' };
  const t = vmGetToken();
  if (t) h['Authorization'] = 'Bearer ' + t;
  return h;
}

/**
 * 带鉴权的 fetch；401 时跳转登录页（保留 next 参数）。
 */
function apiFetch(url, opts) {
  opts = opts || {};
  const headers = Object.assign({}, vmAuthHeaders(), opts.headers || {});
  const nextOpts = Object.assign({ credentials: 'include' }, opts, { headers });
  return fetch(url, nextOpts).then(function (resp) {
    if (resp.status === 401 && !location.pathname.endsWith('/login.html')) {
      var next = encodeURIComponent(location.pathname + location.search);
      location.href = '/login.html?next=' + next;
    }
    return resp;
  });
}
