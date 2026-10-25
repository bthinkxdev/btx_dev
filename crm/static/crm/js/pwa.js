/*
 * BThinkX Business OS — PWA install + Web Push (Part 2 install surface, Part 13.3 push).
 * Pairs with crm/static/crm/js/sw.js (the service worker) and the Alpine `modal` store
 * pattern already used in ui.js. No build step, no dependencies.
 */
(function () {
  'use strict';

  function urlBase64ToUint8Array(base64String) {
    var padding = '='.repeat((4 - (base64String.length % 4)) % 4);
    var base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    var raw = window.atob(base64);
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  /* ------------------------------------------------------------ service worker */
  function registerServiceWorker() {
    if (!('serviceWorker' in navigator)) return;
    navigator.serviceWorker.register('/crm/service-worker.js', { scope: '/crm/' })
      .then(function () {
        document.addEventListener('alpine:initialized', function () {
          var store = window.Alpine && window.Alpine.store('pwa');
          if (store) store.refreshPushState();
        });
      })
      .catch(function (err) { console.warn('Service worker registration failed', err); }); // eslint-disable-line no-console
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', registerServiceWorker);
  else registerServiceWorker();

  /* ------------------------------------------------------------ install prompt capture */
  // beforeinstallprompt can fire before Alpine exists, so stash it on window and sync into
  // the store once Alpine is up (same two-step pattern ui.js uses for the ?modal= deep link).
  window.__crmInstallPrompt = null;
  window.addEventListener('beforeinstallprompt', function (e) {
    e.preventDefault();
    window.__crmInstallPrompt = e;
    var store = window.Alpine && window.Alpine.store('pwa');
    if (store) store.installAvailable = true;
  });
  window.addEventListener('appinstalled', function () {
    window.__crmInstallPrompt = null;
    var store = window.Alpine && window.Alpine.store('pwa');
    if (store) { store.installAvailable = false; store.isStandalone = true; }
    if (window.crmToast) window.crmToast('App installed. You can open it from your home screen.', 'success');
  });

  /* ------------------------------------------------------------ Alpine store */
  document.addEventListener('alpine:init', function () {
    window.Alpine.store('pwa', {
      installAvailable: false,
      isStandalone: window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true,
      isIOS: /iphone|ipad|ipod/i.test(navigator.userAgent) && !window.MSStream,
      pushSupported: 'serviceWorker' in navigator && 'PushManager' in window,
      pushEnabled: false,
      busy: false,

      install: function () {
        var evt = window.__crmInstallPrompt;
        var self = this;
        if (!evt) {
          if (this.isIOS) {
            window.crmToast('Install: tap the Share icon, then "Add to Home Screen".', 'info');
          } else {
            window.crmToast('Open your browser menu and choose "Install app" (or "Add to Home screen").', 'info');
          }
          return;
        }
        evt.prompt();
        evt.userChoice.then(function () {
          window.__crmInstallPrompt = null;
          self.installAvailable = false;
        });
      },

      toggleNotifications: function () {
        if (!this.pushSupported) {
          window.crmToast('Notifications are not supported in this browser.', 'warning');
          return;
        }
        if (this.busy) return;
        if (this.pushEnabled) this.unsubscribe(); else this.subscribe();
      },

      subscribe: function () {
        var self = this;
        var key = document.body.dataset.vapidPublicKey;
        if (!key) {
          window.crmToast('Notifications are not configured for this server yet.', 'warning');
          return;
        }
        if (!('Notification' in window)) {
          window.crmToast('Notifications are not supported in this browser.', 'warning');
          return;
        }
        this.busy = true;
        Notification.requestPermission().then(function (perm) {
          if (perm !== 'granted') {
            self.busy = false;
            window.crmToast('Notifications permission was not granted.', 'warning');
            return;
          }
          navigator.serviceWorker.ready.then(function (reg) {
            return reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(key) });
          }).then(function (sub) {
            return fetch(document.body.dataset.pushSubscribeUrl, {
              method: 'POST', credentials: 'same-origin',
              headers: { 'Content-Type': 'application/json', 'X-CSRFToken': document.body.dataset.csrf },
              body: JSON.stringify(sub.toJSON()),
            });
          }).then(function (r) { return r.json(); }).then(function (res) {
            self.busy = false;
            if (res.ok) { self.pushEnabled = true; window.crmToast(res.message || 'Notifications enabled.', 'success'); }
            else { window.crmToast(res.error || 'Could not enable notifications.', 'error'); }
          }).catch(function () {
            self.busy = false;
            window.crmToast('Could not enable notifications.', 'error');
          });
        });
      },

      unsubscribe: function () {
        var self = this;
        this.busy = true;
        navigator.serviceWorker.ready.then(function (reg) { return reg.pushManager.getSubscription(); })
          .then(function (sub) {
            if (!sub) { self.busy = false; self.pushEnabled = false; return; }
            var endpoint = sub.endpoint;
            return sub.unsubscribe().then(function () {
              return fetch(document.body.dataset.pushUnsubscribeUrl, {
                method: 'POST', credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': document.body.dataset.csrf },
                body: JSON.stringify({ endpoint: endpoint }),
              });
            }).finally(function () {
              self.busy = false;
              self.pushEnabled = false;
              window.crmToast('Notifications turned off on this device.', 'success');
            });
          })
          .catch(function () { self.busy = false; });
      },

      refreshPushState: function () {
        var self = this;
        if (!this.pushSupported) return;
        navigator.serviceWorker.ready.then(function (reg) { return reg.pushManager.getSubscription(); })
          .then(function (sub) { self.pushEnabled = !!sub; })
          .catch(function () {});
      },
    });
  });
})();
