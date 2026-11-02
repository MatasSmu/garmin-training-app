/* Food & weight service worker (scope /food/), same as the gym logger's.
   The page shell is served from the cache first and refreshed in the
   background, so the page opens instantly with no network -- or with network
   but the laptop switched off, where a network-first fetch would just hang.
   API calls are never cached here; the page keeps its own data in IndexedDB.
   Bump VERSION to drop old caches. */
"use strict";

var VERSION = "food-shell-v9";
var SHELL = [
  "/food/",
  "/food/manifest.webmanifest",
  "/static/food/food.css",
  "/static/food/food.js",
  "/static/weight-chart.js",
  "/static/icon-192.png",
  "/static/icon-512.png",
];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(VERSION)
      .then(function (cache) { return cache.addAll(SHELL); })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys()
      .then(function (keys) {
        return Promise.all(keys.filter(function (k) { return k !== VERSION; }).map(function (k) { return caches.delete(k); }));
      })
      .then(function () { return self.clients.claim(); })
  );
});

function refresh(cache, key, request) {
  return fetch(request).then(function (res) {
    if (res && res.ok) cache.put(key, res.clone());
    return res;
  });
}

self.addEventListener("fetch", function (event) {
  var req = event.request;
  if (req.method !== "GET") return;
  var url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  var key = null;
  if (req.mode === "navigate" && url.pathname.indexOf("/food") === 0) key = "/food/";
  else if (SHELL.indexOf(url.pathname) !== -1) key = url.pathname;
  if (!key) return;  // API and everything else: straight to the network

  event.respondWith(
    caches.open(VERSION).then(function (cache) {
      return cache.match(key).then(function (hit) {
        var update = refresh(cache, key, key === "/food/" ? "/food/" : req);
        if (hit) {
          event.waitUntil(update.catch(function () {}));
          return hit;
        }
        return update;
      });
    })
  );
});
