/* Raport cen — skrypt wstawiany inline. Nie wykonuje żadnych żądań sieciowych.
 * Teksty z danych wstawiamy wyłącznie przez textContent (nigdy innerHTML). */
(function () {
  "use strict";

  var rootEl = document.querySelector(".viz-root");
  rootEl.classList.add("js");
  var DATA = JSON.parse(document.getElementById("report-data").textContent);
  var META = DATA.meta;
  var L = META.labels;
  var NBSP = " ";
  var RAMP_STEPS = 7;
  var WEEKDAYS = ["Pn", "Wt", "Śr", "Cz", "Pt", "So", "Nd"];
  var MONTHS = ["sty", "lut", "mar", "kwi", "maj", "cze", "lip", "sie", "wrz", "paź", "lis", "gru"];
  // Indeksy w krotce oferty (zob. reporting/html.py: build_view_data).
  var O = { cat: 0, meal: 1, prep: 2, total: 3, avg: 4, sel: 5, cmp: 6, unit: 7, fcu: 8, compl: 9, disp: 10, idx: 11, cur: 12 };

  var state = { tab: "overview", stat: "min", mode: "lowest", meal: "", prep: "", comp: "", nights: "", date: null, showAll: false };
  // Duże tabele renderujemy partiami; pełna lista po kliknięciu (dane są też w prices.csv).
  var ROW_LIMIT = 1000;

  // ------------------------------------------------------------------ pomocnicze
  function money(g) {
    if (g === null || g === undefined) return "—";
    var sign = g < 0 ? "-" : "";
    var a = Math.abs(g);
    var whole = Math.floor(a / 100);
    var cents = a % 100;
    var digits = String(whole).replace(/\B(?=(\d{3})+(?!\d))/g, NBSP);
    var unit = META.currency === "PLN" ? "zł" : META.currency;
    return sign + digits + "," + (cents < 10 ? "0" : "") + cents + NBSP + unit;
  }
  function median(values) {
    if (!values.length) return null;
    var s = values.slice().sort(function (a, b) { return a - b; });
    var mid = Math.floor(s.length / 2);
    if (s.length % 2) return s[mid];
    return Math.floor((s[mid - 1] + s[mid] + 1) / 2);
  }
  function plDate(iso) {
    var p = iso.split("-");
    return p[2] + "." + p[1] + "." + p[0];
  }
  function weekday(iso) {
    return (new Date(iso + "T00:00:00Z").getUTCDay() + 6) % 7;
  }
  function addDays(iso, n) {
    var d = new Date(iso + "T00:00:00Z");
    d.setUTCDate(d.getUTCDate() + n);
    return d.toISOString().slice(0, 10);
  }
  function label(dict, key) {
    if (key === null || key === undefined) return "—";
    return Object.prototype.hasOwnProperty.call(dict, key) ? dict[key] : key;
  }
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }
  function td(text, cls) { return el("td", cls, text); }
  function nightsLabel(n) { return n + (n === 1 ? " noc" : n < 5 ? " noce" : " nocy"); }
  // Odmiana liczebnika: 1 cena, 2–4 ceny, 5+ cen (z wyjątkiem 12–14).
  function plural(n, one, few, many) {
    if (n === 1) return n + " " + one;
    var last = n % 10, lastTwo = n % 100;
    if (last >= 2 && last <= 4 && (lastTwo < 12 || lastTwo > 14)) return n + " " + few;
    return n + " " + many;
  }
  function pricesLabel(n) {
    return plural(n, "porównywalna cena", "porównywalne ceny", "porównywalnych cen");
  }

  // Ta sama reguła co analytics.comparable_offer: bez filtrów warunków tylko oferty
  // wybrane w kategorii; z filtrem — najtańsza porównywalna spełniająca warunki.
  function comparableOffer(offers, mode, meal, prep) {
    var filtered = !!(meal || prep);
    var best = null;
    for (var i = 0; i < offers.length; i++) {
      var o = offers[i];
      if (!o[O.cmp]) continue;
      if (!filtered && !o[O.sel]) continue;
      if (mode !== "lowest" && o[O.cat] !== mode) continue;
      if (meal && o[O.meal] !== meal) continue;
      if (prep && o[O.prep] !== prep) continue;
      if (best === null) { best = o; continue; }
      var a = [o[O.total] || 0, o[O.avg] || 0, o[O.idx]];
      var b = [best[O.total] || 0, best[O.avg] || 0, best[O.idx]];
      if (a[0] < b[0] || (a[0] === b[0] && (a[1] < b[1] || (a[1] === b[1] && a[2] < b[2])))) best = o;
    }
    return best;
  }

  function competitorVisible(r) { return state.comp === "" || DATA.competitors[r.c][0] === state.comp; }
  function nightsVisible(n) { return state.nights === "" || String(n) === state.nights; }
  function visibleNights() {
    return META.nights.filter(function (n) { return nightsVisible(n); });
  }

  // ------------------------------------------------------------------ statystyki komórek
  function computeCells() {
    var cells = {};
    var useCached = state.comp === "" && !state.meal && !state.prep;
    var nCompetitors = state.comp === "" ? DATA.competitors.length : 1;
    if (useCached) {
      DATA.cells[state.mode].forEach(function (c) {
        cells[c[0] + "|" + c[1]] = { n: c[2], minT: c[3], medT: c[4], minA: c[5], medA: c[6], N: nCompetitors };
      });
      return cells;
    }
    var groups = {};
    DATA.results.forEach(function (r) {
      if (r.a !== "available" || !competitorVisible(r)) return;
      var offer = comparableOffer(r.o, state.mode, state.meal, state.prep);
      if (!offer) return;
      var key = r.d + "|" + r.n;
      var g = groups[key] || (groups[key] = {});
      if (!(r.c in g)) g[r.c] = offer;
    });
    DATA.dates.forEach(function (_iso, d) {
      META.nights.forEach(function (n) {
        var g = groups[d + "|" + n] || {};
        var totals = [], avgs = [];
        Object.keys(g).forEach(function (c) {
          totals.push(g[c][O.total]);
          if (g[c][O.avg] !== null) avgs.push(g[c][O.avg]);
        });
        cells[d + "|" + n] = {
          n: totals.length,
          minT: totals.length ? Math.min.apply(null, totals) : null,
          medT: median(totals),
          minA: avgs.length ? Math.min.apply(null, avgs) : null,
          medA: median(avgs),
          N: nCompetitors
        };
      });
    });
    return cells;
  }

  // ------------------------------------------------------------------ podpowiedź
  var tooltip = document.getElementById("tooltip");
  function showTooltip(target, lines) {
    tooltip.textContent = "";
    lines.forEach(function (line, i) {
      tooltip.appendChild(el("div", i === 0 ? "tt-value" : "tt-label", line));
    });
    tooltip.hidden = false;
    var rect = target.getBoundingClientRect();
    var tw = tooltip.offsetWidth, th = tooltip.offsetHeight;
    var left = Math.min(window.innerWidth - tw - 8, rect.right + 8);
    var top = rect.top - th - 8 < 8 ? rect.bottom + 8 : rect.top - th - 8;
    tooltip.style.left = Math.max(8, left) + "px";
    tooltip.style.top = Math.max(8, top) + "px";
  }
  function hideTooltip() { tooltip.hidden = true; }

  // ------------------------------------------------------------------ kalendarz
  function renderCalendar() {
    var cells = computeCells();
    var metricKey = state.stat === "min" ? "minA" : "medA";
    var container = document.getElementById("calendars");
    container.textContent = "";
    var nightsList = visibleNights();

    var values = [];
    nightsList.forEach(function (n) {
      DATA.dates.forEach(function (_iso, d) {
        var c = cells[d + "|" + n];
        if (c && c[metricKey] !== null) values.push(c[metricKey]);
      });
    });
    var lo = values.length ? Math.min.apply(null, values) : null;
    var hi = values.length ? Math.max.apply(null, values) : null;
    function bin(v) {
      if (hi === lo) return 3;
      return Math.min(RAMP_STEPS - 1, Math.floor(((v - lo) / (hi - lo)) * RAMP_STEPS));
    }

    var metricText = (state.stat === "min" ? "minimum" : "mediana") + " średniej ceny za noc · " +
      label(Object.fromEntries(META.modes), state.mode);
    document.getElementById("cal-metric-label").textContent = metricText;
    renderLegend(lo, hi);

    var first = DATA.dates[0];
    var last = DATA.dates[DATA.dates.length - 1];
    var dateIndex = {};
    DATA.dates.forEach(function (iso, i) { dateIndex[iso] = i; });

    nightsList.forEach(function (n) {
      var panel = el("div", "cal-panel");
      panel.appendChild(el("h3", null, nightsLabel(n)));
      var grid = el("div", "cal-grid");
      grid.setAttribute("role", "grid");
      grid.setAttribute("aria-label", "Kalendarz przyjazdów, " + nightsLabel(n));
      grid.appendChild(el("div", "cal-month", ""));
      WEEKDAYS.forEach(function (w) { grid.appendChild(el("div", "cal-dow", w)); });
      var cursor = addDays(first, -weekday(first));
      while (cursor <= last) {
        var weekDays = [];
        for (var i = 0; i < 7; i++) weekDays.push(addDays(cursor, i));
        var monthLabel = "";
        weekDays.forEach(function (iso) {
          if (iso >= first && iso <= last && (iso === first || iso.slice(8) === "01")) {
            monthLabel = MONTHS[Number(iso.slice(5, 7)) - 1] + " " + iso.slice(0, 4);
          }
        });
        grid.appendChild(el("div", "cal-month", monthLabel));
        weekDays.forEach(function (iso) {
          if (!(iso in dateIndex)) { grid.appendChild(el("div", "cal-blank")); return; }
          var d = dateIndex[iso];
          var c = cells[d + "|" + n] || { n: 0, N: DATA.competitors.length, minT: null, medT: null, minA: null, medA: null };
          var value = c[metricKey];
          var cell = el("button", "cal-cell", String(Number(iso.slice(8))));
          cell.type = "button";
          if (value !== null) {
            var b = bin(value);
            cell.classList.add("has-value");
            cell.style.background = "var(--seq-" + b + ")";
            cell.style.color = b <= 2 ? "var(--seq-ink-low)" : "var(--seq-ink-high)";
            if (c.n < META.threshold) {
              var dot = el("span", "dot");
              dot.setAttribute("aria-hidden", "true");
              cell.appendChild(dot);
            }
          }
          var lines = cellTooltip(iso, n, c, value);
          cell.setAttribute("aria-label", lines.join(". "));
          cell.addEventListener("pointerenter", function () { showTooltip(cell, lines); });
          cell.addEventListener("pointerleave", hideTooltip);
          cell.addEventListener("focus", function () { showTooltip(cell, lines); });
          cell.addEventListener("blur", hideTooltip);
          cell.addEventListener("click", function () {
            state.date = iso;
            state.nights = String(n);
            document.getElementById("f-nights").value = state.nights;
            hideTooltip();
            selectTab("competitors");
          });
          grid.appendChild(cell);
        });
        cursor = addDays(cursor, 7);
      }
      panel.appendChild(grid);
      container.appendChild(panel);
    });
    renderCalendarTable(cells, nightsList);
  }

  function cellTooltip(iso, n, c, value) {
    var lines = [];
    if (value === null) {
      lines.push("Brak porównywalnych cen");
    } else {
      lines.push(money(value) + " za noc");
    }
    lines.push(WEEKDAYS[weekday(iso)] + " " + plDate(iso) + " · " + nightsLabel(n));
    if (c.minT !== null) {
      lines.push("Cena pobytu: min " + money(c.minT) + " · mediana " + money(c.medT));
    }
    var denominator = pricesLabel(c.n) + " / " + c.N + " konkurentów";
    if (c.n > 0 && c.n < META.threshold) denominator += " · mała próba";
    lines.push(denominator);
    if (value !== null) lines.push("Kliknij, aby zobaczyć konkurentów");
    return lines;
  }

  function renderLegend(lo, hi) {
    var legend = document.getElementById("cal-legend");
    legend.textContent = "";
    var key = el("span", "legend-key");
    key.appendChild(el("span", null, lo === null ? "Brak cen" : money(lo)));
    var ramp = el("span", "legend-ramp");
    for (var i = 0; i < RAMP_STEPS; i++) {
      var sw = el("span");
      sw.style.background = "var(--seq-" + i + ")";
      ramp.appendChild(sw);
    }
    key.appendChild(ramp);
    key.appendChild(el("span", null, hi === null ? "" : money(hi) + " za noc"));
    legend.appendChild(key);
    var empty = el("span", "legend-key");
    empty.appendChild(el("span", "legend-swatch"));
    empty.appendChild(el("span", null, "brak porównywalnych cen"));
    legend.appendChild(empty);
    var small = el("span", "legend-key");
    small.appendChild(el("span", "legend-dot"));
    small.appendChild(el("span", null, "mała próba (mniej niż " + META.threshold + " cen)"));
    legend.appendChild(small);
  }

  function renderCalendarTable(cells, nightsList) {
    var body = document.querySelector("#cal-table tbody");
    body.textContent = "";
    var frag = document.createDocumentFragment();
    DATA.dates.forEach(function (iso, d) {
      nightsList.forEach(function (n) {
        var c = cells[d + "|" + n];
        if (!c) return;
        var tr = el("tr");
        tr.appendChild(td(plDate(iso)));
        tr.appendChild(td(String(n), "num"));
        var denom = td(c.n + " / " + c.N);
        if (c.n > 0 && c.n < META.threshold) denom.appendChild(el("span", "tag", "mała próba"));
        tr.appendChild(denom);
        tr.appendChild(td(money(c.minT), "num"));
        tr.appendChild(td(money(c.medT), "num"));
        tr.appendChild(td(money(c.minA), "num"));
        tr.appendChild(td(money(c.medA), "num"));
        frag.appendChild(tr);
      });
    });
    body.appendChild(frag);
  }

  // ------------------------------------------------------------------ konkurenci
  function renderCompetitors() {
    var body = document.querySelector("#comp-table tbody");
    body.textContent = "";
    var frag = document.createDocumentFragment();
    var rows = DATA.results.filter(function (r) {
      return competitorVisible(r) && nightsVisible(r.n) && (state.date === null || DATA.dates[r.d] === state.date);
    });
    rows.sort(function (a, b) {
      return a.d - b.d || a.n - b.n || DATA.competitors[a.c][1].localeCompare(DATA.competitors[b.c][1], "pl");
    });
    var withPrice = 0;
    rows.forEach(function (r) {
      if (r.a === "available" && comparableOffer(r.o, state.mode, state.meal, state.prep)) withPrice++;
    });
    var shown = state.showAll ? rows : rows.slice(0, ROW_LIMIT);
    shown.forEach(function (r) {
      var comp = DATA.competitors[r.c];
      var offer = r.a === "available" ? comparableOffer(r.o, state.mode, state.meal, state.prep) : null;
      var tr = el("tr");
      var nameCell = el("td");
      var link = el("a", null, comp[1]);
      link.href = comp[2];
      link.rel = "noopener noreferrer";
      link.target = "_blank";
      nameCell.appendChild(link);
      nameCell.className = "nowrap";
      tr.appendChild(nameCell);
      tr.appendChild(td(plDate(DATA.dates[r.d]), "nowrap"));
      tr.appendChild(td(String(r.n), "num"));
      tr.appendChild(td(r.t ? label(L.availability, r.a) : label(L.job, r.s)));
      var notes = [];
      if (offer) {
        tr.appendChild(td(DATA.units[offer[O.unit]]));
        tr.appendChild(td(money(offer[O.total]), "num"));
        tr.appendChild(td(money(offer[O.avg]), "num"));
        tr.appendChild(td(label(L.category, offer[O.cat])));
        tr.appendChild(td(offer[O.fcu] || "—"));
        tr.appendChild(td(label(L.meal, offer[O.meal])));
        tr.appendChild(td(label(L.prepayment, offer[O.prep])));
      } else if (r.st) {
        tr.appendChild(td(DATA.units[r.st[5]], "stale"));
        tr.appendChild(td(money(r.st[1]), "num stale"));
        tr.appendChild(td(money(r.st[2]), "num stale"));
        tr.appendChild(td(label(L.category, r.st[4]), "stale"));
        tr.appendChild(td("—"));
        tr.appendChild(td("—"));
        tr.appendChild(td("—"));
        notes.push("Cena nieaktualna — cykl " + r.st[6] + ", odczyt " + r.st[3] + "; nie wchodzi do statystyk");
      } else {
        for (var i = 0; i < 7; i++) tr.appendChild(td(i === 1 || i === 2 ? "—" : "", i === 1 || i === 2 ? "num muted" : "muted"));
      }
      tr.appendChild(td(r.t || "—", "nowrap"));
      if (r.x) notes.push(label(L.nonComparable, r.x));
      if (r.a === "available" && !offer && !r.x) notes.push("Brak oferty spełniającej wybrane warunki");
      if (r.r) notes.push(label(L.reason, r.r));
      if (r.s !== "completed") notes.push("Status: " + label(L.job, r.s));
      if (r.m) notes.push(r.m);
      tr.appendChild(td(notes.join(" · ")));
      frag.appendChild(tr);
    });
    body.appendChild(frag);
    var info = plural(rows.length, "wiersz", "wiersze", "wierszy") + " · " + withPrice + " z porównywalną ceną (" +
      label(Object.fromEntries(META.modes), state.mode) + ")";
    var countEl = document.getElementById("comp-count");
    countEl.textContent = info;
    if (state.date !== null) {
      countEl.appendChild(document.createTextNode(" · przyjazd " + plDate(state.date) + " "));
      var clear = el("button", null, "pokaż wszystkie daty");
      clear.type = "button";
      clear.addEventListener("click", function () { state.date = null; renderCompetitors(); });
      countEl.appendChild(clear);
    }
    appendShowAll(countEl, shown.length, rows.length, renderCompetitors);
  }

  function appendShowAll(container, shown, total, rerender) {
    if (shown >= total) return;
    container.appendChild(document.createTextNode(" · pokazano " + shown + " "));
    var more = el("button", null, "pokaż wszystkie (" + total + ")");
    more.type = "button";
    more.addEventListener("click", function () { state.showAll = true; rerender(); });
    container.appendChild(more);
  }

  // ------------------------------------------------------------------ historia
  function renderHistory() {
    var body = document.querySelector("#history-table tbody");
    body.textContent = "";
    var frag = document.createDocumentFragment();
    var rows = DATA.history.filter(function (h) {
      var r = DATA.results[h[0]];
      return competitorVisible(r) && nightsVisible(r.n) && (state.date === null || DATA.dates[r.d] === state.date);
    });
    rows.sort(function (a, b) {
      var ca = a[2] === null ? -1 : Math.abs(a[2]);
      var cb = b[2] === null ? -1 : Math.abs(b[2]);
      return cb - ca || a[0] - b[0];
    });
    var shown = state.showAll ? rows : rows.slice(0, ROW_LIMIT);
    shown.forEach(function (h) {
      var r = DATA.results[h[0]];
      var tr = el("tr");
      tr.appendChild(td(DATA.competitors[r.c][1], "nowrap"));
      tr.appendChild(td(plDate(DATA.dates[r.d]), "nowrap"));
      tr.appendChild(td(String(r.n), "num"));
      var parts = h[1].map(function (p) {
        var value = p[1] !== null ? money(p[1]) : label(L.availability, p[2]).toLowerCase();
        return value + " (" + (p[3] || "?") + ")";
      });
      tr.appendChild(td(parts.join(" → ")));
      var change = "—";
      if (h[2] !== null) {
        if (h[2] === 0) change = "bez zmian";
        else change = (h[2] < 0 ? "↓ −" : "↑ +") + money(Math.abs(h[2])) +
          (h[3] !== null ? " (" + (h[3] > 0 ? "+" : "−") + String(Math.abs(h[3])).replace(".", ",") + "%)" : "");
      }
      tr.appendChild(td(change, "num"));
      frag.appendChild(tr);
    });
    body.appendChild(frag);
    var countEl = document.getElementById("history-count");
    countEl.textContent = plural(rows.length, "pobyt", "pobyty", "pobytów") + " z co najmniej dwoma odczytami";
    appendShowAll(countEl, shown.length, rows.length, renderHistory);
  }

  // ------------------------------------------------------------------ filtry i zakładki
  function fillSelect(id, entries) {
    var select = document.getElementById(id);
    entries.forEach(function (e) {
      var opt = el("option", null, e[1]);
      opt.value = e[0];
      select.appendChild(opt);
    });
  }
  fillSelect("f-mode", META.modes);
  fillSelect("f-meal", Object.keys(L.meal).filter(function (k) { return k !== "unknown"; }).map(function (k) { return [k, L.meal[k]]; }));
  fillSelect("f-prepay", Object.keys(L.prepayment).filter(function (k) { return k !== "unknown"; }).map(function (k) { return [k, L.prepayment[k]]; }));
  fillSelect("f-comp", DATA.competitors.map(function (c) { return [c[0], c[1]]; }));
  fillSelect("f-nights", META.nights.map(function (n) { return [String(n), nightsLabel(n)]; }));

  function renderActive() {
    if (state.tab === "calendar") renderCalendar();
    else if (state.tab === "competitors") renderCompetitors();
    else if (state.tab === "history") renderHistory();
  }

  [["f-stat", "stat"], ["f-mode", "mode"], ["f-meal", "meal"], ["f-prepay", "prep"], ["f-comp", "comp"], ["f-nights", "nights"]]
    .forEach(function (pair) {
      document.getElementById(pair[0]).addEventListener("change", function (ev) {
        state[pair[1]] = ev.target.value;
        state.showAll = false;
        renderActive();
      });
    });

  var filters = document.getElementById("filters");
  function selectTab(name) {
    state.tab = name;
    state.showAll = false;
    document.querySelectorAll(".tabs [role=tab]").forEach(function (b) {
      b.setAttribute("aria-selected", b.getAttribute("data-tab") === name ? "true" : "false");
    });
    document.querySelectorAll(".tab-panel").forEach(function (p) {
      p.classList.toggle("active", p.id === "tab-" + name);
    });
    filters.hidden = !(name === "calendar" || name === "competitors" || name === "history");
    hideTooltip();
    renderActive();
  }
  document.querySelectorAll(".tabs [role=tab]").forEach(function (b) {
    b.addEventListener("click", function () { selectTab(b.getAttribute("data-tab")); });
  });
  selectTab("overview");
})();
