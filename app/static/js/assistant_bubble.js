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
    if (!node) return;
    node.textContent = text || "";
    if (text !== "Listening...") {
      node.setAttribute("data-ready-status", text || "");
    }
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
    var lastAssistantAnswer = "";

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
    var voiceRecognition = null;
    var voiceListening = false;

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

    function resizeInput() {
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, 120) + "px";
    }

    function appendTranscript(text) {
      var clean = String(text || "").trim();
      if (!clean) return;
      input.value = (input.value.trim() ? input.value.trim() + " " : "") + clean;
      resizeInput();
      input.focus();
    }

    function setVoiceListening(on) {
      voiceListening = !!on;
      button.classList.toggle("is-listening", voiceListening);
      setStatus(status, voiceListening ? "Listening..." : status.getAttribute("data-ready-status") || status.textContent);
    }

    function startVoiceInput() {
      var SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SpeechRecognition) {
        openPanel();
        input.placeholder = "Voice input is not supported in this browser.";
        input.focus();
        return false;
      }
      openPanel();
      if (voiceListening && voiceRecognition) {
        voiceRecognition.stop();
        return true;
      }
      voiceRecognition = new SpeechRecognition();
      voiceRecognition.continuous = true;
      voiceRecognition.interimResults = false;
      voiceRecognition.lang = "en-US";
      voiceRecognition.onresult = function (event) {
        var text = "";
        for (var i = event.resultIndex; i < event.results.length; i++) {
          text += event.results[i][0].transcript;
        }
        appendTranscript(text);
      };
      voiceRecognition.onerror = function () { setVoiceListening(false); };
      voiceRecognition.onend = function () { setVoiceListening(false); };
      try {
        voiceRecognition.start();
        setVoiceListening(true);
        return true;
      } catch (err) {
        setVoiceListening(false);
        return false;
      }
    }

    button.addEventListener("click", function () {
      if (panel.hasAttribute("hidden")) openPanel();
      else closePanel();
    });
    var holdTimer = null;
    var heldForVoice = false;
    button.addEventListener("pointerdown", function (event) {
      if (event.button != null && event.button !== 0) return;
      heldForVoice = false;
      clearTimeout(holdTimer);
      holdTimer = setTimeout(function () {
        heldForVoice = startVoiceInput();
      }, 520);
    }, { passive: true });
    ["pointerup", "pointercancel", "pointerleave"].forEach(function (name) {
      button.addEventListener(name, function () {
        clearTimeout(holdTimer);
        holdTimer = null;
      }, { passive: true });
    });
    button.addEventListener("click", function (event) {
      if (!heldForVoice) return;
      heldForVoice = false;
      event.preventDefault();
      event.stopImmediatePropagation();
    }, true);
    close.addEventListener("click", closePanel);

    input.addEventListener("input", resizeInput);
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        if (form.requestSubmit) form.requestSubmit();
        else send.click();
      }
    });

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var question = input.value.trim();
      if (!question) return;
      var previousQuestion = lastUserQuestion;
      var previousAnswer = lastAssistantAnswer;
      lastUserQuestion = question;
      input.value = "";
      resizeInput();
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
          previous_question: previousQuestion,
          previous_answer: previousAnswer
        })
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          pending.textContent = data && data.answer ? data.answer : "I could not answer that yet.";
          if (data && data.answer) {
            lastAssistantAnswer = data.answer;
          }
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
