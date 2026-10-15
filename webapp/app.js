(function () {
  "use strict";
  var tg = window.Telegram && window.Telegram.WebApp;
  if (tg) {
    tg.ready(); tg.expand();
    try { if (tg.disableVerticalSwipes) tg.disableVerticalSwipes(); } catch (e) { /* старые версии Telegram */ }
    try { tg.setHeaderColor("secondary_bg_color"); } catch (e) { /* не критично */ }
  }
  var backHandler = null;
  // кнопка «Назад» в шапке Telegram: у вложенных экранов ведёт на уровень выше, на вкладках скрыта
  function setBack(fn) {
    if (!tg || !tg.BackButton) return;
    if (backHandler) tg.BackButton.offClick(backHandler);
    backHandler = fn ? function () { fn(); } : null;
    if (backHandler) { tg.BackButton.onClick(backHandler); tg.BackButton.show(); } else tg.BackButton.hide();
  }
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

  // показать экран: пустые элементы (null/false) пропускаются, а не выводятся словом «null»
  function show() { view.replaceChildren.apply(view, Array.prototype.slice.call(arguments).filter(Boolean)); }

  function api(method, path, body) {
    var opts = { method: method, headers: { Authorization: "tma " + (tg ? tg.initData : "") } };
    if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    return fetch(path, opts).then(function (r) {
      return r.json().then(function (data) {
        if (!r.ok) { var e = new Error(data.message || "Ошибка"); e.code = data.error; throw e; }
        return data;
      }, function () { var e = new Error("Сервер ответил не так, как ожидалось. Попробуйте ещё раз."); e.code = "bad_response"; throw e; });
    }, function () { var e = new Error("Нет связи с сервером. Проверьте интернет (или VPN) и повторите."); e.code = "offline"; throw e; });
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
    show(el("div", { class: "error" }, children));
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
    show(
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
      el("h2", { text: "Ещё" }),
      el("div", { class: "grid" }, [
        el("button", { class: "btn ghost", text: "🌐 Каталог", onclick: renderCatalog }),
        el("button", { class: "btn ghost", text: "💎 Тариф", onclick: renderPlans }),
        el("button", { class: "btn ghost", text: "📊 Статистика", onclick: renderStats }),
        el("button", { class: "btn ghost", text: "🤝 Друзья (+дни)", onclick: renderFriends })
      ]),
      el("button", { class: "btn ghost", text: "Вернуться в чат", onclick: closeApp })
    );
  }

  // ---------- Заявки ----------
  var TABS = [["new", "Новые"], ["postponed", "Отложено"], ["taken", "В работе"], ["rejected", "Отказ"], ["spam", "Спам"]];

  function renderLeads() {
    var list = el("div", { id: "leadlist" });
    var more = el("button", { class: "btn ghost", text: "Показать ещё", hidden: "" });
    var tabs = el("div", { class: "tabs" }, TABS.map(function (t) {
      return el("button", { class: "chip" + (t[0] === state.leadsStatus ? " on" : ""), text: t[1], onclick: function () { state.leadsStatus = t[0]; renderLeads(); } });
    }));
    show(el("h1", { text: "Заявки" }), tabs, list, more);
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
    if (state.leadsStatus === "rejected" || state.leadsStatus === "spam") {
      // ошибся кнопкой — заявка возвращается в «Новые» (для «спама» снимается и голос)
      card.appendChild(el("div", { class: "row" }, [act("restore", "↩️ Вернуть в новые")]));
    }
    return card;
  }

  // ---------- Профиль ----------
  function field(label, node, hint) {
    return el("label", { class: "field" }, [el("span", { text: label }), node, hint ? el("small", { class: "muted", text: hint }) : null]);
  }

  function renderProfile() {
    show(el("h1", { text: "Профиль" }), el("p", { class: "muted center-block", text: "Загрузка…" }));
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

    show(
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

  // ---------- Клиенты (CRM) ----------
  var crmState = { stage: "all", q: "" };

  function fmtDate(iso) {
    // сервер отдаёт время в UTC без пояса
    var d = new Date(iso.indexOf("Z") === -1 && iso.indexOf("+") === -1 ? iso + "Z" : iso);
    return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  }

  function renderClients() {
    setBack(null);
    var list = el("div", {});
    var more = el("button", { class: "btn ghost", text: "Показать ещё", hidden: "" });
    var chipsBox = el("div", { class: "tabs scroll" });
    var search = el("input", { type: "text", value: crmState.q, placeholder: "🔎 Имя, @username или телефон", maxlength: "64" });
    search.addEventListener("keydown", function (e) { if (e.key === "Enter") { crmState.q = search.value.trim(); renderClients(); } });
    var addBtn = el("button", { class: "btn small", text: "➕ Добавить клиента", onclick: renderClientForm });
    show(el("h1", { text: "Клиенты" }), search, chipsBox, list, more, el("div", { class: "row" }, [addBtn]));
    var offset = 0;
    function page() {
      more.disabled = true;
      api("GET", "/api/clients?stage=" + crmState.stage + "&q=" + encodeURIComponent(crmState.q) + "&offset=" + offset).then(function (res) {
        if (!offset) {
          var total = Object.keys(res.counts).reduce(function (a, k) { return a + res.counts[k]; }, 0);
          var chips = [["all", "Все " + total]].concat(res.stages.map(function (s) { return [s.id, s.title + " " + (res.counts[s.id] || 0)]; }));
          chipsBox.replaceChildren.apply(chipsBox, chips.map(function (c) {
            return el("button", { class: "chip" + (c[0] === crmState.stage ? " on" : ""), text: c[1], onclick: function () { crmState.stage = c[0]; renderClients(); } });
          }));
          if (!res.items.length) list.appendChild(el("p", { class: "muted center-block", text: crmState.q ? "Ничего не найдено." : "Клиентов пока нет. Нажмите «✅ В работу» под заявкой или добавьте вручную." }));
        }
        var names = {}; res.stages.forEach(function (s) { names[s.id] = s.title; });
        res.items.forEach(function (c) { list.appendChild(clientRow(c, names)); offset += 1; });
        more.hidden = !res.has_more; more.disabled = false;
      }).catch(function (e) { toast(e.message, true); more.disabled = false; });
    }
    more.onclick = page;
    page();
  }

  function clientRow(c, names) {
    var who = c.name || (c.username ? "@" + c.username : "") || c.phone || "Без имени";
    var sub = [names[c.stage] || c.stage, c.budget ? c.budget.toLocaleString("ru-RU") + " ₽" : "", c.source].filter(Boolean).join(" · ");
    return el("div", { class: "card tap", onclick: function () { renderClient(c.id); } }, [el("b", { text: who }), el("div", { class: "muted", text: sub })]);
  }

  function renderClientForm() {
    setBack(renderClients);
    var name = el("input", { type: "text", maxlength: "255", placeholder: "Имя" });
    var user = el("input", { type: "text", maxlength: "64", placeholder: "@username" });
    var phone = el("input", { type: "text", maxlength: "32", placeholder: "Телефон" });
    var ok = el("button", { class: "btn", text: "Добавить", onclick: function () {
      ok.disabled = true;
      api("POST", "/api/clients", { name: name.value, username: user.value, phone: phone.value })
        .then(function (c) { toast("Клиент добавлен"); renderClient(c.id); })
        .catch(function (e) { toast(e.message, true); ok.disabled = false; });
    } });
    show(el("h1", { text: "Новый клиент" }), field("Имя", name), field("@username", user), field("Телефон", phone), ok,
      el("button", { class: "btn ghost", text: "Назад", onclick: renderClients }));
  }

  function renderClient(id) {
    setBack(renderClients);
    show(el("p", { class: "muted center-block", text: "Загрузка…" }));
    api("GET", "/api/clients/" + id).then(function (c) { buildClient(c); }).catch(function (e) { toast(e.message, true); renderClients(); });
  }

  function buildClient(c) {
    setBack(renderClients);
    var STAGES = [["new", "🆕 Новый"], ["talk", "💬 В переговорах"], ["thinking", "🤔 Думает"], ["payment", "💳 Оплата"], ["done", "✅ Выполнено"], ["lost", "❌ Отказ"]];
    function patch(body, msg) {
      api("PATCH", "/api/clients/" + c.id, body).then(function (n) { toast(msg || "Сохранено"); buildClient(n); }).catch(function (e) { toast(e.message, true); });
    }
    var stageChips = el("div", { class: "chips" }, STAGES.map(function (s) {
      return el("button", { class: "chip" + (s[0] === c.stage ? " on" : ""), type: "button", text: s[1], onclick: function () { if (s[0] !== c.stage) patch({ stage: s[0] }, "Этап изменён"); } });
    }));
    var budget = el("input", { type: "text", inputmode: "numeric", value: c.budget ? String(c.budget) : "", placeholder: "Бюджет, ₽" });
    var phone = el("input", { type: "text", maxlength: "32", value: c.phone, placeholder: "Телефон" });
    var name = el("input", { type: "text", maxlength: "255", value: c.name, placeholder: "Имя" });
    var note = el("textarea", { rows: "2", maxlength: "1000", placeholder: "Новая заметка" });
    var rem = [1, 3, 7].map(function (d) { return el("button", { class: "btn small ghost", text: "⏰ " + d + " дн", onclick: function () { patch({ remind_days: d }, "Напоминание поставлено"); } }); });
    show(
      el("h1", { text: c.name || (c.username ? "@" + c.username : "Клиент") }),
      el("div", { class: "muted", text: [c.username ? "@" + c.username : "", c.source, c.category].filter(Boolean).join(" · ") }),
      el("h2", { text: "Этап" }), stageChips,
      c.contact ? el("div", { class: "row" }, [el("button", { class: "btn small", text: "✍️ Написать", onclick: function () { openContact(c.contact); } })]) : null,
      el("h2", { text: "Данные" }),
      field("Имя", name), field("Бюджет", budget), field("Телефон", phone),
      el("button", { class: "btn small", text: "Сохранить данные", onclick: function () {
        var body = {}; if (budget.value.trim()) body.budget = budget.value; if (phone.value.trim()) body.phone = phone.value;
        if (name.value.trim() && name.value.trim() !== c.name) body.name = name.value;
        patch(body);
      } }),
      el("h2", { text: "Напоминание" }),
      c.reminder ? el("div", { class: "muted", text: "Ближайшее: " + fmtDate(c.reminder) }) : el("div", { class: "muted", text: "Не поставлено" }),
      el("div", { class: "row" }, rem.concat(c.reminder ? [el("button", { class: "btn small ghost", text: "🔕 Убрать", onclick: function () { patch({ remind_off: true }, "Напоминание отменено"); } })] : [])),
      el("h2", { text: "Заметки" }),
      c.notes ? el("pre", { class: "replytext", text: c.notes }) : el("div", { class: "muted", text: "Пока пусто" }),
      note,
      el("button", { class: "btn small", text: "Добавить заметку", onclick: function () { if (note.value.trim()) patch({ note: note.value }, "Заметка добавлена"); } }),
      el("h2", { text: "История" }),
      el("div", {}, c.events.map(function (e) { return el("div", { class: "muted", text: fmtDate(e.at) + " · " + e.text }); })),
      el("button", { class: "btn ghost", text: "◀️ К списку", onclick: renderClients }),
      el("button", { class: "btn ghost danger", text: "🗑 Удалить клиента", onclick: function () {
        confirmAsk("Удалить клиента вместе с историей и напоминаниями? Вернуть будет нельзя.", function () {
          api("DELETE", "/api/clients/" + c.id).then(function () { toast("Клиент удалён"); renderClients(); })
            .catch(function (e) { toast(e.message, true); });
        });
      } })
    );
  }

  // ---------- Каталог ----------
  var catTab = "mine";

  function openLink(url) {
    if (tg && url.indexOf("https://t.me/") === 0) tg.openTelegramLink(url);
    else if (tg && tg.openLink) tg.openLink(url);
    else window.open(url, "_blank");
  }

  function groupCard(g) {
    var kind = g.kind === "channel" ? "📢 канал" : "👥 группа";
    var sub = [kind, g.members + " уч.", g.city, g.leads ? "заявок: " + g.leads : ""].filter(Boolean).join(" · ");
    return el("div", { class: "card" + (g.link ? " tap" : ""), onclick: g.link ? function () { openLink(g.link); } : null }, [
      el("b", { text: (g.ad ? "📢 " : "") + g.title }), el("div", { class: "muted", text: sub })
    ]);
  }

  function renderCatalog() {
    setBack(function () { go("home"); });
    var body = el("div", {});
    var tabs = el("div", { class: "tabs" }, [["mine", "По моим нишам"], ["all", "Весь каталог"], ["my", "Мои группы"], ["top", "🏆 Рейтинг"]].map(function (x) {
      return el("button", { class: "chip" + (x[0] === catTab ? " on" : ""), text: x[1], onclick: function () { catTab = x[0]; renderCatalog(); } });
    }));
    show(el("h1", { text: "🌐 Каталог" }), tabs, body, backHome());
    if (catTab === "mine" || catTab === "all") {
      api("GET", "/api/catalog?scope=" + catTab).then(function (r) {
        body.appendChild(el("div", { class: "card" }, [
          el("p", { text: "Ваш уровень: " + r.level.name + " (заявок из ваших групп: " + r.level.leads + ")" }),
          el("p", { class: "muted", text: r.level.next }),
          el("p", { class: "muted", text: "🎁 За каждые " + r.bonus_leads + " заявок из вашей группы — +1 день 💎." })
        ]));
        if (!r.groups.length) body.appendChild(el("p", { class: "muted center-block", text: "Пока пусто. Станьте первым — добавьте группу в боте." }));
        r.groups.forEach(function (g) { body.appendChild(groupCard(g)); });
        body.appendChild(el("button", { class: "btn ghost", text: "➕ Добавить группу (в боте)", onclick: closeApp }));
      }).catch(function (e) { toast(e.message, true); });
    } else if (catTab === "my") {
      api("GET", "/api/catalog/my").then(function (r) {
        body.appendChild(el("div", { class: "muted", text: "🎁 Получено бонусных дней: " + r.bonus_days + " · Уровень: " + r.level }));
        if (!r.items.length) body.appendChild(el("p", { class: "muted center-block", text: "Вы пока не добавляли группы. Добавить: в боте 🌐 Каталог → ➕." }));
        r.items.forEach(function (g) {
          var parts = [el("b", { text: g.title }), el("div", { class: "muted", text: g.status + (g.in_catalog ? " · заявок: " + g.leads : "") + (g.bonus_days ? " · бонус: +" + g.bonus_days + " дн." : "") })];
          if (g.ad_active) parts.push(el("div", { class: "muted", text: "📢 Реклама идёт до " + new Date(g.ad_until).toLocaleDateString("ru-RU") }));
          if (g.in_catalog && r.ad_cost > 0) {
            parts.push(g.ad_pending ? el("div", { class: "muted", text: "⏳ Заявка на рекламу ждёт ответа" })
              : el("button", { class: "btn small", text: "📢 Реклама (" + r.ad_cost + " заявок, доступно " + g.ad_available + ")", onclick: function () {
                api("POST", "/api/catalog/ads/" + g.id).then(function (x) { toast(x.message); renderCatalog(); }).catch(function (e) { toast(e.message, true); });
              } }));
          }
          body.appendChild(el("div", { class: "card" }, parts));
        });
      }).catch(function (e) { toast(e.message, true); });
    } else {
      api("GET", "/api/rating?scope=all").then(function (r) {
        if (!r.top.length) body.appendChild(el("p", { class: "muted center-block", text: "Пока никого — станьте первым!" }));
        var medals = ["🥇", "🥈", "🥉"];
        r.top.forEach(function (x) {
          body.appendChild(el("div", { class: "card" + (x.me ? " me" : "") }, [
            el("b", { text: (medals[x.place - 1] || x.place + ".") + " " + x.name + (x.me ? " ← вы" : "") }),
            el("div", { class: "muted", text: x.level + " · заявок: " + x.leads })
          ]));
        });
        if (r.place && r.place > 10) body.appendChild(el("p", { class: "muted", text: "Ваше место: " + r.place }));
      }).catch(function (e) { toast(e.message, true); });
    }
  }

  function backHome() { return el("button", { class: "btn ghost", text: "◀️ Назад", onclick: function () { go("home"); } }); }

  // ---------- Тариф и оплата ----------
  // мини-радар: чем длиннее тариф, тем больше «целей» (30 дн. — 3, до 90 — 6, дольше — 10), как на картинке в окне оплаты
  var RADAR_DOTS = [[30, .55], [115, .45], [230, .65], [75, .85], [165, .8], [300, .5], [345, .8], [260, .9], [195, .35], [20, .95]];
  function radar(days) {
    var n = days <= 30 ? 3 : days <= 90 ? 6 : 10, NS = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 100 100"); svg.setAttribute("width", "44"); svg.setAttribute("height", "44");
    svg.setAttribute("aria-hidden", "true"); svg.setAttribute("class", "radar");
    function add(tag, attrs) {
      var n2 = document.createElementNS(NS, tag);
      Object.keys(attrs).forEach(function (k) { n2.setAttribute(k, attrs[k]); });
      svg.appendChild(n2);
    }
    [48, 32, 16].forEach(function (r) { add("circle", { cx: 50, cy: 50, r: r, fill: "none", stroke: "currentColor", "stroke-opacity": .55, "stroke-width": 2 }); });
    add("path", { d: "M50 50 L66 4 A48 48 0 0 1 93 30 Z", fill: "currentColor", "fill-opacity": .3 });
    RADAR_DOTS.slice(0, n).forEach(function (d) {
      var a = d[0] * Math.PI / 180;
      add("circle", { cx: 50 + Math.sin(a) * 48 * d[1], cy: 50 - Math.cos(a) * 48 * d[1], r: 4.5, fill: "#ffcc1a" });
    });
    return svg;
  }

  function renderPlans() {
    setBack(function () { go("home"); });
    show(el("p", { class: "muted center-block", text: "Загрузка…" }));
    api("GET", "/api/plans").then(function (r) {
      var parts = [el("h1", { text: "💎 Тариф" }), el("div", { class: "card" }, [
        el("p", { text: r.intro }),
        r.paid ? el("p", { class: "muted", text: r.until ? "Действует до " + new Date(r.until).toLocaleDateString("ru-RU") : "Бессрочно" }) : null
      ])];
      if (r.renew) {
        parts.push(el("h2", { text: "Привязанная карта" }), el("div", { class: "card" }, [
          el("b", { text: "💳 " + r.renew.card }),
          el("p", { class: "muted", text: "Автопродление: каждые " + r.renew.days + " дн. Перед окончанием тарифа спишем стоимость пакета (за 3 дня предупредим). Отвязать карту можно в любой момент." }),
          el("button", { class: "btn small", text: "🗑 Отвязать карту", onclick: function () {
            confirmAsk("Отвязать карту " + r.renew.card + "? Автопродление отключится, списаний больше не будет. Оплаченный тариф останется до конца срока.", function () {
              api("POST", "/api/renew/off").then(function (x) { toast(x.message); renderPlans(); }).catch(function (e) { toast(e.message, true); });
            });
          } })
        ]));
      }
      if (r.packages.length && r.can_pay) {
        parts.push(el("h2", { text: "Продлить или купить" }));
        r.packages.forEach(function (p) {
          parts.push(el("button", { class: "btn pkg", onclick: function () {
            api("POST", "/api/pay", { days: p.days }).then(function (x) { openLink(x.url); toast("Откроется страница оплаты (СБП или карта). Тариф включится сам в течение минуты."); })
              .catch(function (e) { toast(e.message, true); });
          } }, [radar(p.days), el("span", { text: p.days + " дней — " + p.price.toLocaleString("ru-RU") + " ₽" })]));
        });
      } else if (!r.paid) {
        parts.push(el("p", { class: "muted", text: "Оплата пока не подключена. Больше заявок можно получить за приглашения друзей и добавленные группы." }));
      }
      parts.push(el("p", { class: "muted", text: r.refund }), backHome());
      show.apply(null, parts);
    }).catch(function (e) { toast(e.message, true); go("home"); });
  }

  // ---------- Статистика ----------
  function renderStats() {
    setBack(function () { go("home"); });
    show(el("p", { class: "muted center-block", text: "Загрузка…" }));
    api("GET", "/api/stats").then(function (r) {
      show(
        el("h1", { text: "📊 Статистика" }),
        el("div", { class: "grid" }, [stat(r.today, "за сутки"), stat(r.week, "за неделю"), stat(r.total, "всего"), stat(r.taken, "в работу")]),
        el("p", { class: "muted", text: "Конверсия в работу: " + (r.conversion === null ? "—" : r.conversion + "%") }),
        el("h2", { text: "Клиенты по этапам" }),
        r.stages.length ? el("div", {}, r.stages.map(function (x) { return el("div", { class: "card" }, [el("b", { text: String(x.count) + " · " }), el("span", { text: x.title })]); })) : el("p", { class: "muted", text: "Пока нет." }),
        el("p", { text: "💰 Сумма выполненных сделок: " + r.done_sum.toLocaleString("ru-RU") + " ₽" }),
        backHome()
      );
    }).catch(function (e) { toast(e.message, true); go("home"); });
  }

  // ---------- Друзья ----------
  function renderFriends() {
    setBack(function () { go("home"); });
    show(el("p", { class: "muted center-block", text: "Загрузка…" }));
    api("GET", "/api/referrals").then(function (r) {
      show(
        el("h1", { text: "🤝 Пригласить друзей" }),
        el("div", { class: "card" }, [
          el("p", { text: "Друг заходит по вашей ссылке и настраивает бота — вы оба получаете +" + r.days + " дней 💎." }),
          el("p", { class: "muted", text: "Дальше, когда он зарабатывает бонусные дни за свои группы, вам идёт ещё " + r.percent + "% от них." })
        ]),
        el("div", { class: "grid" }, [stat(r.invited, "приглашено"), stat(r.done, "настроили бота")]),
        el("h2", { text: "Ваша ссылка" }),
        el("pre", { class: "replytext", text: r.link }),
        el("button", { class: "btn", text: "📤 Отправить друзьям", onclick: function () { openContact(r.share); } }),
        el("button", { class: "btn ghost", text: "📋 Скопировать ссылку", onclick: function () { copyText(r.link); } }),
        el("button", { class: "btn ghost", text: "◀️ Назад", onclick: function () { go("home"); } })
      );
    }).catch(function (e) { toast(e.message, true); go("home"); });
  }

  // ---------- Управление (только админ) ----------
  var adminTab = "status";

  function renderAdmin() {
    setBack(null);
    var body = el("div", {});
    var tabs = el("div", { class: "tabs" }, [["status", "Состояние"], ["settings", "Настройки"], ["users", "Пользователи"]].map(function (x) {
      return el("button", { class: "chip" + (x[0] === adminTab ? " on" : ""), text: x[1], onclick: function () { adminTab = x[0]; renderAdmin(); } });
    }));
    show(el("h1", { text: "🛠 Управление" }), tabs, body);
    if (adminTab === "status") adminStatus(body);
    else if (adminTab === "settings") adminSettings(body);
    else adminUsers(body);
  }

  function flag(ok) { return ok ? "✅" : "⬜"; }

  function adminStatus(body) {
    body.appendChild(el("p", { class: "muted center-block", text: "Загрузка…" }));
    api("GET", "/api/admin/status").then(function (r) {
      var d = r.status;
      var accs = Object.keys(d.accounts).map(function (k) { return k + " " + d.accounts[k]; }).join(", ") || "нет";
      var nodes = [
        el("div", { class: "grid" }, [stat(d.users, "зарегистрировано"), stat(d.paid, "на 💎"), stat(d.leads_day, "заявок за сутки"), stat(d.sent_day, "рассылок за сутки")]),
        el("div", { class: "card" }, [
          el("b", { text: "🔍 Разведчик: " + (d.scout_paused ? "⏸ на паузе" : "▶️ работает") }),
          el("div", { class: "muted", text: "режим " + d.mode + "; аккаунты: " + accs }),
          el("button", { class: "btn small", text: d.scout_paused ? "▶️ Возобновить" : "⏸ Поставить на паузу", onclick: function () {
            confirmAsk(d.scout_paused ? "Возобновить разведчика?" : "Поставить разведчика на паузу?", function () {
              api("PUT", "/api/admin/scout", { paused: !d.scout_paused }).then(function () { toast("Готово"); renderAdmin(); }).catch(function (e) { toast(e.message, true); });
            });
          } })
        ]),
        el("div", { class: "card" }, [
          el("div", { text: flag(d.selling) + " Продажа 💎: " + (d.selling ? d.prices.join(" / ") + " ₽" : "не продаётся (цены 0)") }),
          el("div", { text: flag(d.pay_ready) + " Ключи оплаты: " + (d.pay_ready ? "есть" : "нет") }),
          el("div", { text: flag(d.refund_days !== "0") + " Возврат: " + (d.refund_days !== "0" ? d.refund_days + " дн." : "выключен") }),
          el("div", { text: flag(d.autorenew) + " Автопродление  " + flag(d.license) + " Лицензия  " + flag(d.ads) + " Реклама" }),
          el("div", { text: flag(d.ai) + " Claude (платно)" + (d.ai ? ", модель " + d.ai_model : "") + (d.ai && !d.ai_key ? " — ⚠️ нет ключа" : "") }),
          el("div", { text: flag(!!d.stats_channel) + " Канал статистики: " + (d.stats_channel || "не подключён") }),
          d.memory ? el("div", { text: ({ ok: "✅", warn: "⚠️", bad: "🔴" })[d.memory.level] + " Память: свободно " + d.memory.available_mb + " из " + d.memory.total_mb + " МБ, подкачка " + d.memory.swap_used_mb + " МБ" + (d.memory.bot_mb !== null ? ", бот " + d.memory.bot_mb + " МБ" : "") }) : null
        ])
      ];
      if (r.bad.length) nodes.push(el("h2", { text: "Исправить" }), el("div", { class: "card" }, r.bad.map(function (x) { return el("div", { text: "⚠️ " + x }); })));
      nodes.push(el("h2", { text: "В порядке" }), el("div", { class: "card" }, r.ok.map(function (x) { return el("div", { class: "muted", text: "✅ " + x }); })));
      nodes.push(el("h2", { text: "Пресеты" }));
      r.presets.forEach(function (pr) {
        var parts = [el("b", { text: pr.icon + " " + pr.title }), el("div", { class: "muted", text: pr.desc })];
        if (!pr.changes.length) parts.push(el("div", { class: "muted", text: "Уже выставлено так." }));
        else {
          pr.changes.forEach(function (c) { parts.push(el("div", { class: "muted", text: "• " + c.label + ": " + (c.old || "—") + " → " + c.new })); });
          parts.push(el("button", { class: "btn small", text: "Применить (" + pr.changes.length + ")", onclick: function () {
            confirmAsk("Пресет «" + pr.title + "» изменит настроек: " + pr.changes.length + ". Применить?", function () {
              api("POST", "/api/admin/presets/" + pr.id).then(function (x) { toast("Изменено настроек: " + x.changed); renderAdmin(); }).catch(function (e) { toast(e.message, true); });
            });
          } }));
        }
        nodes.push(el("div", { class: "card" }, parts));
      });
      body.replaceChildren.apply(body, nodes);
    }).catch(function (e) { toast(e.message, true); });
  }

  function adminSettings(body) {
    body.appendChild(el("p", { class: "muted center-block", text: "Загрузка…" }));
    api("GET", "/api/admin/settings").then(function (r) {
      var nodes = [];
      r.sections.forEach(function (sec) {
        nodes.push(el("h2", { text: sec.icon + " " + sec.title }));
        sec.items.forEach(function (it) { nodes.push(settingRow(it)); });
      });
      body.replaceChildren.apply(body, nodes);
    }).catch(function (e) { toast(e.message, true); });
  }

  function settingRow(it) {
    var valueEl = el("b", { text: it.value === "" ? "выкл" : it.value });
    var editor = null;
    var head = el("div", { class: "lead-head tap", onclick: function () {
      if (editor) { editor.remove(); editor = null; return; }
      var input = el("input", { type: "text", value: it.value, maxlength: "64" });
      function save(v) {
        api("PUT", "/api/admin/settings/" + it.key, { value: v }).then(function (x) {
          it.value = x.value; valueEl.textContent = x.value === "" ? "выкл" : x.value; toast("Сохранено");
          if (editor) { editor.remove(); editor = null; }
        }).catch(function (e) { toast(e.message, true); });
      }
      editor = el("div", { class: "reply" }, [
        el("div", { text: it.label }), el("div", { class: "muted", text: it.hint }),
        el("div", { class: "muted", text: "По умолчанию: " + (it.default === "" ? "выкл" : it.default) }), input,
        el("div", { class: "row" }, [
          el("button", { class: "btn small", text: "Сохранить", onclick: function () { save(input.value); } }),
          el("button", { class: "btn small ghost", text: "↩️ По умолчанию", onclick: function () { save(it.default); } })
        ])
      ]);
      row.appendChild(editor);
    } }, [el("span", { text: it.short }), valueEl]);
    var row = el("div", { class: "card" }, [head]);
    return row;
  }

  var adminQuery = "";

  function adminUsers(body) {
    var list = el("div", {});
    var more = el("button", { class: "btn ghost", text: "Показать ещё", hidden: "" });
    var search = el("input", { type: "text", value: adminQuery, placeholder: "🔎 ID, @ник или имя", maxlength: "64" });
    search.addEventListener("keydown", function (e) { if (e.key === "Enter") { adminQuery = search.value.trim(); renderAdmin(); } });
    body.replaceChildren(search, list, more);
    var offset = 0;
    function page() {
      more.disabled = true;
      api("GET", "/api/admin/users?q=" + encodeURIComponent(adminQuery) + "&offset=" + offset).then(function (r) {
        if (!offset) list.appendChild(el("p", { class: "muted", text: "Всего пользователей: " + r.total }));
        if (!offset && !r.items.length) list.appendChild(el("p", { class: "muted center-block", text: "Никого не найдено." }));
        r.items.forEach(function (u) {
          var title = (u.paid ? "💎 " : "") + (u.blocked ? "⛔ " : "") + (u.onboarded ? "" : "⚪ ") + (u.name || u.id);
          list.appendChild(el("div", { class: "card tap", onclick: function () { adminUser(u.id); } }, [el("b", { text: title }), el("div", { class: "muted", text: [u.username ? "@" + u.username : "", u.city].filter(Boolean).join(" · ") })]));
          offset += 1;
        });
        more.hidden = !r.has_more; more.disabled = false;
      }).catch(function (e) { toast(e.message, true); more.disabled = false; });
    }
    more.onclick = page;
    page();
  }

  function adminUser(id) {
    setBack(renderAdmin);
    show(el("p", { class: "muted center-block", text: "Загрузка…" }));
    api("GET", "/api/admin/users/" + id).then(buildAdminUser).catch(function (e) { toast(e.message, true); renderAdmin(); });
  }

  function buildAdminUser(u) {
    setBack(renderAdmin);
    function plan(value, text) {
      confirmAsk(text + " Пользователю " + (u.name || u.id) + "?", function () {
        api("POST", "/api/admin/users/" + u.id + "/plan", { value: value }).then(function (n) { toast("Тариф обновлён"); buildAdminUser(n); }).catch(function (e) { toast(e.message, true); });
      });
    }
    var tariff = u.paid ? "💎 Полный" + (u.until ? " до " + new Date(u.until).toLocaleDateString("ru-RU") : " бессрочно") : "🆓 Бесплатный";
    show(
      el("h1", { text: u.name || String(u.id) }),
      el("div", { class: "muted", text: [u.username ? "@" + u.username : "", "ID " + u.id, u.admin ? "администратор" : ""].filter(Boolean).join(" · ") }),
      el("div", { class: "card" }, [
        el("div", { text: "Тариф: " + tariff }), el("div", { text: "Город: " + (u.city || "вся Россия") }),
        el("div", { text: "Ниши: " + (u.niches.join(", ") || "—") }),
        el("div", { text: "Заявок: сегодня " + u.today + ", всего " + u.total + " · клиентов: " + u.clients }),
        el("div", { class: "muted", text: "С нами с " + new Date(u.since).toLocaleDateString("ru-RU") + (u.blocked ? " · ⛔ заблокировал бота" : "") })
      ]),
      el("h2", { text: "Тариф" }),
      el("div", { class: "row" }, [
        el("button", { class: "btn small", text: "💎 +30 дней", onclick: function () { plan("30", "Добавить 30 дней 💎."); } }),
        el("button", { class: "btn small", text: "💎 +365 дней", onclick: function () { plan("365", "Добавить 365 дней 💎."); } })
      ]),
      el("div", { class: "row" }, [
        el("button", { class: "btn small ghost", text: "💎 Бессрочно", onclick: function () { plan("0", "Сделать 💎 бессрочным."); } }),
        el("button", { class: "btn small ghost", text: "🆓 Бесплатный", onclick: function () { plan("free", "Перевести на бесплатный тариф."); } })
      ]),
      el("button", { class: "btn ghost", text: "◀️ К списку", onclick: renderAdmin })
    );
  }

  // ---------- каркас ----------
  function renderSoon(title) {
    show(
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
    setBack(null);
    var s = state.me.sections.filter(function (x) { return x.id === state.tab; })[0];
    if (!s) state.tab = "home";
    if (state.tab === "home") renderHome();
    else if (state.tab === "leads") renderLeads();
    else if (state.tab === "profile") renderProfile();
    else if (state.tab === "clients") renderClients();
    else if (state.tab === "admin") renderAdmin();
    else renderSoon(s.title);
    nav.replaceChildren.apply(nav, state.me.sections.map(function (x) {
      return el("button", { "aria-current": String(x.id === state.tab), onclick: function () { go(x.id); if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged(); } },
        [el("div", { text: ICONS[x.id] || "•" }), el("div", { text: x.title })]);
    }));
    nav.hidden = false;
    window.scrollTo(0, 0);
  }

  function load() {
    show(el("p", { class: "muted center", text: "Загрузка…" }));
    api("GET", "/api/me").then(function (me) { state.me = me; render(); }).catch(showError);
  }
  load();
})();
