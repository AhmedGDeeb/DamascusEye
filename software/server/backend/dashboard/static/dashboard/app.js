// Damascus Eye dashboard: small progressive enhancements, no framework.

(function () {
  'use strict';

  // Light / dark theme. With no saved choice the page follows the OS setting;
  // the inline script in <head> applies a saved choice before first paint.
  var root = document.documentElement;
  document.querySelectorAll('[data-theme-toggle]').forEach(function (button) {
    button.addEventListener('click', function () {
      var dark = root.dataset.theme
        ? root.dataset.theme === 'dark'
        : window.matchMedia('(prefers-color-scheme: dark)').matches;
      root.dataset.theme = dark ? 'light' : 'dark';
      try { localStorage.setItem('theme', root.dataset.theme); } catch (e) {}
    });
  });

  // Copy-to-clipboard buttons: <button data-copy="#element-id">
  document.querySelectorAll('[data-copy]').forEach(function (button) {
    button.addEventListener('click', function () {
      var source = document.querySelector(button.dataset.copy);
      if (!source) return;
      var original = button.textContent;
      navigator.clipboard.writeText(source.textContent.trim()).then(function () {
        button.textContent = button.dataset.copied || '✓';
        setTimeout(function () { button.textContent = original; }, 1600);
      });
    });
  });

  // Confirm before destructive submits: <form data-confirm="...">
  document.querySelectorAll('form[data-confirm]').forEach(function (form) {
    form.addEventListener('submit', function (event) {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  // Auto-submit filter forms on change.
  document.querySelectorAll('form[data-autosubmit]').forEach(function (form) {
    form.querySelectorAll('select, input[type=date]').forEach(function (input) {
      input.addEventListener('change', function () { form.submit(); });
    });
  });

  // Sidewalk zone editor: click on the image to add points (normalized 0-1).
  var editor = document.getElementById('zone-editor');
  if (editor) {
    var input = document.getElementById(editor.dataset.input);
    var svg = editor.querySelector('svg');
    var polygon = svg.querySelector('polygon');
    var points = [];
    try { points = JSON.parse(input.value || '[]'); } catch (e) { points = []; }

    var render = function () {
      polygon.setAttribute('points', points.map(function (p) { return p[0] + ',' + p[1]; }).join(' '));
      svg.querySelectorAll('circle').forEach(function (c) { c.remove(); });
      points.forEach(function (p) {
        var circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        circle.setAttribute('cx', p[0]);
        circle.setAttribute('cy', p[1]);
        circle.setAttribute('r', 0.008);
        svg.appendChild(circle);
      });
      input.value = JSON.stringify(points);
      var counter = document.getElementById('zone-count');
      if (counter) counter.textContent = points.length;
    };

    editor.addEventListener('click', function (event) {
      var rect = editor.getBoundingClientRect();
      var x = (event.clientX - rect.left) / rect.width;
      var y = (event.clientY - rect.top) / rect.height;
      points.push([Math.round(x * 10000) / 10000, Math.round(y * 10000) / 10000]);
      render();
    });
    document.getElementById('zone-undo').addEventListener('click', function () { points.pop(); render(); });
    document.getElementById('zone-clear').addEventListener('click', function () { points = []; render(); });
    render();
  }

  // Node map (Leaflet is loaded only on the nodes page).
  var mapEl = document.getElementById('map');
  if (mapEl) {
    var data = JSON.parse(document.getElementById('map-nodes').textContent);
    if (typeof L === 'undefined') {
      mapEl.innerHTML = '<p class="empty"></p>';
      mapEl.firstChild.textContent = mapEl.dataset.offlineText;
    } else if (!data.length) {
      mapEl.innerHTML = '<p class="empty"></p>';
      mapEl.firstChild.textContent = mapEl.dataset.emptyText;
    } else {
      var map = L.map(mapEl);
      L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 19, attribution: '&copy; OpenStreetMap'
      }).addTo(map);
      var bounds = [];
      data.forEach(function (n) {
        var marker = L.circleMarker([n.lat, n.lng], {
          radius: 9, weight: 2, color: '#fff',
          fillColor: n.online ? '#24794a' : '#b4372b', fillOpacity: 1
        }).addTo(map);
        var label = document.createElement('a');
        label.href = n.url;
        label.textContent = n.code + (n.name ? ' — ' + n.name : '');
        marker.bindPopup(label);
        bounds.push([n.lat, n.lng]);
      });
      map.fitBounds(bounds, { padding: [40, 40], maxZoom: 17 });
    }
  }

  // Live image list (AJAX): every few seconds ask the server for images newer than
  // the newest card, and for cards that were still waiting for YOLO. The server
  // returns ready-made card HTML (dashboard:capture_feed), so no page reload is needed.
  var grid = document.getElementById('capture-grid');
  if (grid && grid.dataset.feedUrl) {
    var badge = document.getElementById('live-badge');
    var latestId = parseInt(grid.dataset.latestId, 10) || 0;
    var period = parseInt(grid.dataset.poll, 10) || 5000;
    var MAX_CARDS = 48;
    var newCount = 0;
    var busy = false;

    var setBadge = function (state) {
      if (!badge) return;
      var label = badge.dataset['label' + state.charAt(0).toUpperCase() + state.slice(1)];
      if (state === 'live' && newCount) label += ' · ' + newCount + ' ' + badge.dataset.labelNew;
      badge.className = 'live-badge' + (state === 'live' ? '' : ' paused');
      badge.lastElementChild.textContent = label;
    };

    var toElement = function (html) {
      var template = document.createElement('template');
      template.innerHTML = html.trim();
      return template.content.firstElementChild;
    };

    var waitingIds = function () {
      return Array.prototype.map.call(
        grid.querySelectorAll('[data-status="pending"], [data-status="processing"]'),
        function (card) { return card.dataset.id; }
      ).slice(0, 100);
    };

    var poll = function () {
      if (busy || document.hidden) return;
      busy = true;
      var url = grid.dataset.feedUrl + (grid.dataset.feedUrl.indexOf('?') < 0 ? '?' : '&') +
        'after=' + latestId + '&watch=' + waitingIds().join(',');
      fetch(url, { credentials: 'same-origin', cache: 'no-store', headers: { 'Accept': 'application/json' } })
        .then(function (response) {
          // An expired session redirects to the HTML login page instead of JSON.
          if ((response.headers.get('Content-Type') || '').indexOf('application/json') < 0) throw new Error('login');
          if (!response.ok) throw new Error('http ' + response.status);
          return response.json();
        })
        .then(function (data) {
          data.changed.forEach(function (item) {
            var old = grid.querySelector('[data-id="' + item.id + '"]');
            if (old) old.replaceWith(toElement(item.html));
          });
          if (data['new'].length) {
            var empty = grid.querySelector('[data-empty]');
            if (empty) empty.remove();
            // The server sends newest first; insert oldest first so the newest ends on top.
            data['new'].slice().reverse().forEach(function (item) {
              if (grid.querySelector('[data-id="' + item.id + '"]')) return;
              var card = toElement(item.html);
              card.classList.add('is-new');
              grid.prepend(card);
              newCount += 1;
            });
            var cards = grid.querySelectorAll('.capture-card');
            for (var i = MAX_CARDS; i < cards.length; i++) cards[i].remove();
          }
          latestId = Math.max(latestId, data.latest_id);
          setBadge('live');
        })
        .catch(function (error) { setBadge(error.message === 'login' ? 'login' : 'offline'); })
        .then(function () { busy = false; });
    };

    setInterval(poll, period);
    // Catch up immediately when the tab becomes visible again.
    document.addEventListener('visibilitychange', function () { if (!document.hidden) poll(); });
  }
})();
