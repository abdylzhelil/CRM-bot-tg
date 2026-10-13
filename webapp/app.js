(function () {
  "use strict";
  var tg = window.Telegram && window.Telegram.WebApp;
  if (tg) { tg.ready(); tg.expand(); }
  var view = document.getElementById("view");
  var nav = document.getElementById("nav");
  var toastEl = document.getElementById("toast");
  var ICONS = { home: "🏠", leads: "📥", clients: "👥", profile: "👤", admin: "🛠" };
  var state = { me: null, tab: "home", leadsStatus: "new" };

  // ---------- утилиты ----------
  function el(tag, attrs, children) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (k === "text") n.textContent = v;
      else if (k === "class") n.className = v;
      else if (k === "value") n.value = v;
      else if (k === "checked") n.checked = !!v;
      else if (k.indexOf("on") === 0) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v);
    });
    (children || []).forEach(function (c) { if (c) n.appendChild(c); });
    return n;
  }

  function api(method, path, body) {
    var opts = { method: method, headers: { Authorization: "tma " + (tg ? tg.initData : "") } };
    if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    return fetch(path, opts).then(function (r) {
      return r.json().then(function (data) {
        if (!r.ok) { var e = new Error(data.message || "Ошибка"); e.code = data.error; throw e; }
        return data;
      });
    });
  }

  var toastTimer;
  function toast(text, bad) {
    toastEl.textContent = text;
    toastEl.className = "show" + (bad ? " bad" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.className = ""; }, 2600);
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred(bad ? "error" : "success");
  }

  function closeApp() { if (tg) tg.close(); }

  function openContact(url) {
    if (tg && url.indexOf("https://t.me/") === 0) tg.openTelegramLink(url);
    else window.open(url, "_blank");
  }

  function copyText(text) {
    function fallback() {
      var ta = el("textarea", { value: text, class: "offscreen" });
      document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); toast("Скопировано"); } catch (e) { toast("Не удалось скопировать — выделите текст вручную", true); }
      ta.remove();
    }
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(function () { toast("Скопировано"); }, fallback);
    else fallback();
  }

  function confirmAsk(text, yes) {
    if (tg && tg.showConfirm) tg.showConfirm(text, function (ok) { if (ok) yes(); });
    else if (window.confirm(text)) yes();
  }

  function showError(e) {
    nav.hidden = true;
    var children = [el("p", { text: e.message || "Не удалось загрузить" })];
    if (e.code === "unauthorized" || e.code === "not_registered") children.push(el("button", { class: "btn", text: "Закрыть и открыть бота", onclick: closeApp }));
    else children.push(el("button", { class: "btn ghost", text: "Повторить", onclick: load }));
    view.replaceChildren(el("div", { class: "error" }, children));
  }

  // ---------- Главная ----------
  function roleBadge(me) {
    var map = { admin: ["Администратор", "admin"], paid: ["💎 Полный тариф", "paid"], free: ["Бесплатный тариф", ""] };
    return el("span", { class: "badge " + map[me.role][1], text: map[me.role][0] });
  }
  function planLine(me) {
    if (me.role === "admin") return "Заявки приходят все и сразу.";
    if (me.plan.paid) return me.plan.until ? "Тариф действует до " + new Date(me.plan.until).toLocaleDateString("ru-RU") + ". Заявки сразу и без лимита." : "Бессрочный тариф. Заявки сразу и без лимита.";
    return "До " + me.plan.free_limit + " заявок в день, с задержкой " + me.plan.free_delay_min + " мин.";
  }
  function stat(n, label, go) {
    return el("div", { class: "stat" + (go ? " tap" : ""), onclick: go || null }, [el("b", { text: String(n) }), el("span", { text: label })]);
  }
  function renderHome() {
    var me = state.me;
    view.replaceChildren(
      el("h1", { text: "Привет, " + (me.name || "друг") + "!" }),
      el("div", {}, [roleBadge(me)]),
      el("p", { class: "muted", text: planLine(me) }),
      me.paused ? el("div", { class: "error", text: "⏸ Заявки на паузе. Возобновить: вкладка «Профиль»." }) : null,
      el("h2", { text: "Заявки" }),
      el("div", { class: "grid" }, [
        stat(me.stats.today, "за сутки"),
        stat(me.stats.new, "новых", function () { state.leadsStatus = "new"; go("leads"); }),
        stat(me.stats.taken, "в работе", function () { state.leadsStatus = "taken"; go("leads"); }),
        stat(me.stats.clients, "клиентов")
      ]),
      el("button", { class: "btn ghost", text: "Вернуться в чат", onclick: closeApp })
    );
  }

  // ---------- Заявки ----------
  var TABS = [["new", "Новые"], ["postponed", "Отложено"], ["taken", "В работе"], ["rejected", "Отказ"]];

  function renderLeads() {
    var list = el("div", { id: "leadlist" });
    var more = el("button", { class: "btn ghost", text: "Показать ещё", hidden: "" });
    var tabs = el("div", { class: "tabs" }, TABS.map(function (t) {
      return el("button", { class: "chip" + (t[0] === state.leadsStatus ? " on" : ""), text: t[1], onclick: function () { state.leadsStatus = t[0]; renderLeads(); } });
    }));
    view.replaceChildren(el("h1", { text: "Заявки" }), tabs, list, more);
    var lastId = 0;
    function fetchPage() {
      var q = "/api/leads?status=" + state.leadsStatus + (lastId ? "&before=" + lastId : "");
      more.disabled = true;
      api("GET", q).then(function (res) {
        if (!lastId && !res.items.length) list.appendChild(el("p", { class: "muted center-block", text: "Здесь пока пусто." }));
        res.items.forEach(function (it) { list.appendChild(leadCard(it)); lastId = it.id; });
        more.hidden = !res.has_more; more.disabled = false;
      }).catch(function (e) { toast(e.message, true); more.disabled = false; });
    }
    more.onclick = fetchPage;
    fetchPage();
  }

  function leadCard(it) {
    var card = el("div", { class: "card lead" });
    var head = it.suspicious ? "⚠️ Подозрительный" : (it.hot ? "🔥 Горячий" : "🆕 Новый");
    var who = (it.name || "—") + (it.username ? " (@" + it.username + ")" : "");
    var reply = el("div", { class: "reply", hidden: "" });

    function act(action, label) {
      return el("button", { class: "btn small", text: label, onclick: function () {
        function run() {
          api("POST", "/api/leads/" + it.id + "/" + action).then(function (r) {
            toast(r.note);
            card.remove();
          }).catch(function (e) { toast(e.message, true); });
        }
        if (action === "spam") confirmAsk("Отметить как спам? Автор больше не придёт.", run); else run();
      } });
    }
    function showReply(kind, title) {
      api("GET", "/api/leads/" + it.id + "/reply?kind=" + kind).then(function (r) {
        reply.hidden = false;
        reply.replaceChildren(
          el("div", { class: "muted", text: title }),
          el("pre", { class: "replytext", text: r.text }),
          el("div", { class: "row" }, [
            el("button", { class: "btn small", text: "📋 Скопировать", onclick: function () { copyText(r.text); } }),
            el("button", { class: "btn small ghost", text: "✍️ Написать", onclick: function () { openContact(r.contact); } })
          ])
        );
      }).catch(function (e) { toast(e.message, true); });
    }

    card.appendChild(el("div", { class: "lead-head" }, [el("b", { text: head }), el("span", { class: "muted", text: it.category })]));
    card.appendChild(el("div", { class: "muted", text: "Кто: " + who }));
    card.appendChild(el("div", { class: "muted", text: "Группа: " + it.group + (it.place ? " · " + it.place : "") }));
    card.appendChild(el("p", { class: "lead-text", text: it.text }));
    if (it.from_colleague) card.appendChild(el("div", { class: "muted", text: "📨 Передано коллегой" }));
    card.appendChild(el("div", { class: "row" }, [
      el("button", { class: "btn small", text: "✍️ Написать", onclick: function () { openContact(it.contact); } }),
      el("button", { class: "btn small ghost", text: "📋 Шаблон", onclick: function () { showReply("template", "Шаблон с вашими ценами"); } }),
      el("button", { class: "btn small ghost", text: "✨ Черновик", onclick: function () { showReply("draft", "Черновик по правилам — поправьте под себя"); } })
    ]));
    card.appendChild(reply);
    if (state.leadsStatus === "new" || state.leadsStatus === "postponed") {
      var row = [act("take", "✅ В работу")];
      if (state.leadsStatus === "new") row.push(act("later", "⏰ Отложить"));
      row.push(act("reject", "❌ Отказ"), act("spam", "🚫 Спам"));
      card.appendChild(el("div", { class: "row" }, row));
      if (it.suspicious && state.me.role === "admin") card.appendChild(el("div", { class: "row" }, [act("client", "👍 Это клиент")]));
    }
    return card;
  }

  // ---------- Профиль ----------
  function field(label, node, hint) {
    return el("label", { class: "field" }, [el("span", { text: label }), node, hint ? el("small", { class: "muted", text: hint }) : null]);
  }

  function renderProfile() {
    view.replaceChildren(el("h1", { text: "Профиль" }), el("p", { class: "muted center-block", text: "Загрузка…" }));
    Promise.all([api("GET", "/api/profile"), api("GET", "/api/autosetup")]).then(function (res) {
      buildProfile(res[0], res[1]);
    }).catch(showError);
  }

  function buildProfile(p, auto) {
    var chosen = {};
    p.niches.forEach(function (n) { if (n.on) chosen[n.id] = true; });
    var chips = el("div", { class: "chips" }, p.niches.map(function (n) {
      var b = el("button", { class: "chip" + (n.on ? " on" : ""), type: "button", text: n.emoji + " " + n.name });
      b.onclick = function () { if (chosen[n.id]) delete chosen[n.id]; else chosen[n.id] = true; b.className = "chip" + (chosen[n.id] ? " on" : ""); };
      return b;
    }));
    var city = el("input", { type: "text", maxlength: "64", value: p.city, placeholder: "Пусто — вся Россия" });
    var only = el("input", { type: "checkbox", checked: p.only_city });
    var quiet = el("input", { type: "text", maxlength: "8", value: p.quiet_hours, placeholder: "например 23-8" });
    var negs = el("textarea", { rows: "2", placeholder: "бесплатно, вакансия" }); negs.value = p.negatives.join(", ");
    var tpl = el("textarea", { rows: "4", maxlength: "2000", placeholder: "Здравствуйте{, имя}! {цены}" }); tpl.value = p.reply_template;
    var prices = el("textarea", { rows: "2", maxlength: "2000", placeholder: "лендинг от 15 000 ₽, магазин от 40 000 ₽" }); prices.value = p.prices;
    var paused = el("input", { type: "checkbox", checked: p.paused });
    var save = el("button", { class: "btn", text: "Сохранить" });

    save.onclick = function () {
      save.disabled = true;
      api("PUT", "/api/profile", {
        niches: Object.keys(chosen).map(Number), city: city.value, only_city: only.checked, quiet_hours: quiet.value,
        negatives: negs.value.split(/[,\n]/).map(function (x) { return x.trim(); }).filter(Boolean),
        reply_template: tpl.value, prices: prices.value, paused: paused.checked
      }).then(function () { toast("Сохранено"); state.me.paused = paused.checked; save.disabled = false; })
        .catch(function (e) { toast(e.message, true); save.disabled = false; });
    };

    var autoBox = null;
    if (auto.plan.length || auto.missing.length) {
      var body = [el("b", { text: "⚡ Автонастройка" }), el("p", { class: "muted", text: auto.intro })];
      if (auto.plan.length) {
        body.push(el("p", { text: "Заполню то, что у вас пусто:" }));
        auto.plan.forEach(function (x) { body.push(el("div", { class: "muted", text: "• " + x.what + ": " + x.value })); });
        body.push(el("button", { class: "btn", text: "Применить", onclick: function () {
          api("POST", "/api/autosetup").then(function () { toast("Готово"); renderProfile(); }).catch(function (e) { toast(e.message, true); });
        } }));
      }
      if (auto.missing.length) {
        body.push(el("p", { text: "Осталось выбрать самому:" }));
        auto.missing.forEach(function (m) { body.push(el("div", { class: "muted", text: "• " + m })); });
      }
      autoBox = el("div", { class: "card" }, body);
    }

    view.replaceChildren(
      el("h1", { text: "Профиль" }),
      autoBox,
      el("h2", { text: "Ниши" }), chips,
      el("h2", { text: "Где искать" }),
      field("Город", city),
      el("label", { class: "check" }, [only, el("span", { text: "Только мой город (без заявок без адреса)" })]),
      el("h2", { text: "Фильтры" }),
      field("Режим тишины", quiet, "С какого по какой час копить обычные заявки. 🔥 Горячие придут сразу. Пусто — выключен."),
      field("Минус-слова", negs, "Заявки с этими словами не придут. Через запятую."),
      el("h2", { text: "Ответ клиенту" }),
      field("Мой шаблон", tpl, "Можно {имя}, {цены}, {группа}, {категория}."),
      field("Мои цены", prices),
      el("label", { class: "check" }, [paused, el("span", { text: "⏸ Пауза: не присылать заявки" })]),
      save
    );
  }

  // ---------- каркас ----------
  function renderSoon(title) {
    view.replaceChildren(
      el("h1", { text: title }),
      el("div", { class: "card" }, [
        el("p", { text: "Этот раздел кабинета появится в ближайших обновлениях." }),
        el("p", { class: "muted", text: "Пока он работает в боте — всё доступно там, ничего не потеряется." })
      ]),
      el("button", { class: "btn", text: "Открыть чат с ботом", onclick: closeApp })
    );
  }

  function go(tab) { state.tab = tab; render(); }

  function render() {
    var s = state.me.sections.filter(function (x) { return x.id === state.tab; })[0];
    if (!s) state.tab = "home";
    if (state.tab === "home") renderHome();
    else if (state.tab === "leads") renderLeads();
    else if (state.tab === "profile") renderProfile();
    else renderSoon(s.title);
    nav.replaceChildren.apply(nav, state.me.sections.map(function (x) {
      return el("button", { "aria-current": String(x.id === state.tab), onclick: function () { go(x.id); if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged(); } },
        [el("div", { text: ICONS[x.id] || "•" }), el("div", { text: x.title })]);
    }));
    nav.hidden = false;
    window.scrollTo(0, 0);
  }

  function load() {
    view.replaceChildren(el("p", { class: "muted center", text: "Загрузка…" }));
    api("GET", "/api/me").then(function (me) { state.me = me; render(); }).catch(showError);
  }
  load();
})();
