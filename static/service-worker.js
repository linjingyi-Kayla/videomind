/* global self, clients */
self.addEventListener('push', function (event) {
  let payload = {};
  if (event.data) {
    try {
      payload = event.data.json();
    } catch (e) {
      payload = { body: event.data.text() };
    }
  }

  const title = payload.title || 'VideoMind 提醒';
  const body = payload.body || '';
  const taskId = payload.task_id || '';
  const url = payload.url || '';

  const options = {
    body,
    tag: taskId || 'videomind',
    data: { taskId, url },
    renotify: false
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  const taskId = event.notification.data && event.notification.data.taskId;
  const targetUrl = '/?task_id=' + encodeURIComponent(taskId || '');

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (windowClients) {
      if (windowClients && windowClients.length > 0) {
        return windowClients[0].focus().then(function () {
          // 通过打开新窗口兜底，确保能带上 task_id 参数
          return clients.openWindow(targetUrl);
        });
      }
      return clients.openWindow(targetUrl);
    })
  );
});

