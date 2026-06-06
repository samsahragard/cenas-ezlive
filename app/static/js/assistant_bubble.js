(function () {
  try {
    if (window.self !== window.top) return;
  } catch (err) {
    return;
  }

  function dedupeRoots() {
    var roots = Array.prototype.slice.call(document.querySelectorAll(".ckai-root"));
    roots.slice(1).forEach(function (node) {
      node.remove();
    });
    return roots[0] || null;
  }

  if (window.__cenasAssistantLoaded) {
    dedupeRoots();
    return;
  }
  window.__cenasAssistantLoaded = true;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text) node.textContent = text;
    return node;
  }

  function addMessage(list, role, text) {
    var row = el("div", "ckai-msg ckai-" + role);
    row.textContent = text || "";
    list.appendChild(row);
    list.scrollTop = list.scrollHeight;
    return row;
  }

  function setStatus(node, text) {
    if (node) node.textContent = text || "";
  }

  function init() {
    if (dedupeRoots()) return;

    var root = el("div", "ckai-root");
    root.setAttribute("hidden", "hidden");
    var button = el("button", "ckai-orb");
    button.type = "button";
    button.setAttribute("aria-label", "Open Cenas assistant");
    button.innerHTML = '<span class="ckai-orb-core"></span><span class="ckai-orb-ring"></span>';

    var panel = el("section", "ckai-panel");
    panel.setAttribute("aria-label", "Cenas assistant");
    panel.setAttribute("hidden", "hidden");

    var header = el("div", "ckai-head");
    var titleWrap = el("div", "ckai-title-wrap");
    titleWrap.appendChild(el("div", "ckai-title", "Cenas AI"));
    var status = el("div", "ckai-status", "Checking access...");
    titleWrap.appendChild(status);
    header.appendChild(titleWrap);
    var close = el("button", "ckai-close", "x");
    close.type = "button";
    close.setAttribute("aria-label", "Close assistant");
    header.appendChild(close);

    var messages = el("div", "ckai-messages");
    var intro = addMessage(messages, "assistant", "Ask me a Cenas question. I will only answer what your role is allowed to see.");
    var lastUserQuestion = "";

    var form = el("form", "ckai-form");
    var input = el("textarea", "ckai-input");
    input.name = "question";
    input.rows = 2;
    input.maxLength = 2000;
    input.placeholder = "Ask a question...";
    var send = el("button", "ckai-send", "Ask");
    send.type = "submit";
    form.appendChild(input);
    form.appendChild(send);

    panel.appendChild(header);
    panel.appendChild(messages);
    panel.appendChild(form);
    root.appendChild(panel);
    root.appendChild(button);
    document.body.appendChild(root);

    fetch("/assistant/context", {
      headers: {
        "X-Current-Path": window.location.pathname + window.location.search + window.location.hash
      }
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data || !data.ok || !data.enabled) {
          root.setAttribute("hidden", "hidden");
          return;
        }
        root.removeAttribute("hidden");
        var principal = data.principal || {};
        var role = principal.role || "user";
        var activeTools = (data.tools || []).filter(function (tool) {
          return tool.available;
        });
        setStatus(status, role + " - " + activeTools.length + " active " + (activeTools.length === 1 ? "tool" : "tools"));
        intro.textContent = "Ask me a Cenas question. Partner catalog tools are available by role; unimplemented tools are saved for Sam review.";
      })
      .catch(function () {
        setStatus(status, "Review-gated");
      });

    function openPanel() {
      panel.removeAttribute("hidden");
      button.classList.add("is-open");
      setTimeout(function () { input.focus(); }, 40);
    }
    function closePanel() {
      panel.setAttribute("hidden", "hidden");
      button.classList.remove("is-open");
    }

    button.addEventListener("click", function () {
      if (panel.hasAttribute("hidden")) openPanel();
      else closePanel();
    });
    close.addEventListener("click", closePanel);

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var question = input.value.trim();
      if (!question) return;
      var previousQuestion = lastUserQuestion;
      lastUserQuestion = question;
      input.value = "";
      addMessage(messages, "user", question);
      var pending = addMessage(messages, "assistant", "Thinking...");
      send.disabled = true;
      fetch("/assistant/ask", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Current-Path": window.location.pathname + window.location.search + window.location.hash
        },
        body: JSON.stringify({
          question: question,
          previous_question: previousQuestion
        })
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          pending.textContent = data && data.answer ? data.answer : "I could not answer that yet.";
          if (data && data.queued) {
            pending.classList.add("ckai-queued");
          }
        })
        .catch(function () {
          pending.textContent = "I could not reach the assistant right now.";
          pending.classList.add("ckai-queued");
        })
        .finally(function () {
          send.disabled = false;
          input.focus();
        });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
