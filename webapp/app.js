(function () {
  "use strict";
  var tg = window.Telegram && window.Telegram.WebApp;
  if (tg) { tg.ready(); tg.expand(); }
  var view = document.getElementById("view");
  var nav = document.getElementById("nav");
  var ICONS = { home: "🏠", leads: "📥", clients: "👥", profile: "👤", admin: "🛠" };
  var state = { me: null, tab: "home" };

  function el(tag, attrs, children) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") n.textContent = attrs[k];
      else if (k === "class") n.className = attrs[k];
      else if (k.indexOf("on") === 0) n.addEventListener(k.slice(2), attrs[k]);
      else n.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { n.appendChild(c); });
    return n;
  }

  function api(path) {
    return fetch(path, { headers: { Authorization: "tma " + (tg ? tg.initData : "") } }).then(function (r) {
      return r.json().then(function (body) {
        if (!r.ok) { var e = new Error(body.message || "Ошибка"); e.code = body.error; throw e; }
        return body;
      });
    });
  }

  function showError(e) {
    nav.hidden = true;
    var children = [el("p", { text: e.message || "Не удалось загрузить" })];
    if (e.code === "unauthorized" || e.code === "not_registered") {
      children.push(el("button", { class: "btn", text: "Закрыть и открыть бота", onclick: function () { tg ? tg.close() : null; } }));
    } else {
      children.push(el("button", { class: "btn ghost", text: "Повторить", onclick: load }));
    }
    view.replaceChildren(el("div", { class: "error" }, children));
  }

  function roleBadge(me) {
    var map = { admin: ["Администратор", "admin"], paid: ["💎 Полный тариф", "paid"], free: ["Бесплатный тариф", ""] };
    return el("span", { class: "badge " + map[me.role][1], text: map[me.role][0] });
  }

  function planLine(me) {
    if (me.role === "admin") return "Заявки приходят все и сразу.";
    if (me.plan.paid) {
      return me.plan.until ? "Тариф действует до " + new Date(me.plan.until).toLocaleDateString("ru-RU") + ". Заявки сразу и без лимита."
        : "Бессрочный тариф. Заявки сразу и без лимита.";
    }
    return "До " + me.plan.free_limit + " заявок в день, с задержкой " + me.plan.free_delay_min + " мин.";
  }

  function stat(n, label) { return el("div", { class: "stat" }, [el("b", { text: String(n) }), el("span", { text: label })]); }

  function renderHome() {
    var me = state.me;
    view.replaceChildren(
      el("h1", { text: "Привет, " + (me.name || "друг") + "!" }),
      el("div", {}, [roleBadge(me)]),
      el("p", { class: "muted", text: planLine(me) }),
      me.paused ? el("div", { class: "error", text: "⏸ Заявки на паузе. Возобновить можно в боте: 👤 Профиль." }) : el("span"),
      el("h2", { text: "Заявки" }),
      el("div", { class: "grid" }, [stat(me.stats.today, "за сутки"), stat(me.stats.new, "новых"), stat(me.stats.taken, "в работе"), stat(me.stats.clients, "клиентов")]),
      el("button", { class: "btn ghost", text: "Вернуться в чат", onclick: function () { tg ? tg.close() : null; } })
    );
  }

  function renderSoon(id, title) {
    view.replaceChildren(
      el("h1", { text: title }),
      el("div", { class: "card" }, [
        el("p", { text: "Этот раздел кабинета появится в ближайших обновлениях." }),
        el("p", { class: "muted", text: "Пока он работает в боте — всё доступно там, ничего не потеряется." })
      ]),
      el("button", { class: "btn", text: "Открыть чат с ботом", onclick: function () { tg ? tg.close() : null; } })
    );
  }

  function render() {
    var s = state.me.sections.filter(function (x) { return x.id === state.tab; })[0];
    if (state.tab === "home" || !s) renderHome(); else renderSoon(s.id, s.title);
    nav.replaceChildren.apply(nav, state.me.sections.map(function (x) {
      return el("button", { "aria-current": String(x.id === state.tab), onclick: function () { state.tab = x.id; render(); if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged(); } },
        [el("div", { text: ICONS[x.id] || "•" }), el("div", { text: x.title })]);
    }));
    nav.hidden = false;
  }

  function load() {
    view.replaceChildren(el("p", { class: "muted center", text: "Загрузка…" }));
    api("/api/me").then(function (me) { state.me = me; render(); }).catch(showError);
  }
  load();
})();
