(function () {
  "use strict";

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function mix(a, b, t) {
    return Math.round(a + (b - a) * t);
  }

  function rgba(hexA, hexB, t, alpha) {
    var a = hexA.match(/\w\w/g).map(function (v) { return parseInt(v, 16); });
    var b = hexB.match(/\w\w/g).map(function (v) { return parseInt(v, 16); });
    return "rgba(" + mix(a[0], b[0], t) + "," + mix(a[1], b[1], t) + "," + mix(a[2], b[2], t) + "," + alpha + ")";
  }

  function isAssistantSurface() {
    var path = window.location.pathname || "";
    var params = new URLSearchParams(window.location.search || "");
    return path.indexOf("/assistant") !== -1 ||
      path.indexOf("/sam/chat") !== -1 ||
      params.get("tab") === "cena";
  }

  function setupOrb(canvas, seedBase) {
    if (!canvas || canvas.__ckAiOrbReady) return;
    canvas.__ckAiOrbReady = true;

    var ctx = canvas.getContext("2d");
    if (!ctx) return;

    var cssSize = parseInt(canvas.getAttribute("data-size") || "78", 10) || 78;
    var dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = cssSize * dpr;
    canvas.height = cssSize * dpr;
    canvas.style.width = cssSize + "px";
    canvas.style.height = cssSize + "px";
    ctx.scale(dpr, dpr);

    var clickedUntil = 0;
    var clickTarget = canvas.closest("a, button");
    if (clickTarget) {
      clickTarget.addEventListener("click", function () {
        clickedUntil = performance.now() + 2200;
      });
    }

    var seed = seedBase || 37;
    function rnd() {
      seed = (seed * 1664525 + 1013904223) >>> 0;
      return seed / 4294967296;
    }

    var dots = [];
    for (var i = 0; i < 230; i++) {
      dots.push({
        angle: rnd() * Math.PI * 2,
        radius: Math.pow(rnd(), 0.54) * (cssSize * 0.44),
        size: 0.45 + rnd() * 1.15,
        speed: 0.2 + rnd() * 1.15,
        wobble: rnd() * Math.PI * 2,
        lobe: 2 + Math.floor(rnd() * 5)
      });
    }

    function drawLightning(cx, cy, t, active, beat) {
      var bolts = active ? 5 : 4;
      ctx.save();
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      ctx.shadowBlur = active ? 11 : 8;
      ctx.shadowColor = active ? rgba("ff2a2a", "2880ff", beat, 0.9) : "rgba(255,255,255,.8)";

      for (var b = 0; b < bolts; b++) {
        var span = cssSize * (0.1 + 0.12 * Math.sin(t * 1.7 + b));
        var angle = t * (1.7 + b * 0.11) + b * 2.09;
        var x = cx + Math.cos(angle) * span;
        var y = cy + Math.sin(angle * 1.17) * span;
        var length = cssSize * (0.16 + 0.08 * Math.sin(t * 2.4 + b * 1.3));
        var alpha = 0.36 + 0.35 * Math.pow(Math.sin(t * 9.0 + b), 2);
        ctx.strokeStyle = active
          ? rgba("ff3434", "2f7eff", (beat + b * 0.17) % 1, alpha)
          : "rgba(255,255,255," + alpha + ")";
        ctx.lineWidth = active ? 1.35 : 1.05;
        ctx.beginPath();
        ctx.moveTo(x, y);
        for (var s = 1; s <= 5; s++) {
          var p = s / 5;
          var zig = Math.sin(t * 14 + b * 8 + s * 2.3) * cssSize * 0.035;
          var px = x + Math.cos(angle + 0.45) * length * p + Math.cos(angle + Math.PI / 2) * zig;
          var py = y + Math.sin(angle + 0.45) * length * p + Math.sin(angle + Math.PI / 2) * zig;
          ctx.lineTo(px, py);
        }
        ctx.stroke();
      }
      ctx.restore();
    }

    function frame(now) {
      var t = now * 0.001;
      var cx = cssSize / 2;
      var cy = cssSize / 2;
      var active = isAssistantSurface() || now < clickedUntil ||
        (clickTarget && clickTarget.classList && clickTarget.classList.contains("active"));
      var beat = (Math.sin(t * 3.4) + 1) / 2;
      var pulse = active ? beat : 0;

      ctx.clearRect(0, 0, cssSize, cssSize);

      var glow = ctx.createRadialGradient(cx, cy, 1, cx, cy, cssSize * 0.56);
      if (active) {
        glow.addColorStop(0, rgba("ff2020", "1e78ff", pulse, 0.55));
        glow.addColorStop(0.42, rgba("ff2020", "1e78ff", 1 - pulse, 0.2));
      } else {
        glow.addColorStop(0, "rgba(255,255,255,.42)");
        glow.addColorStop(0.42, "rgba(255,255,255,.13)");
      }
      glow.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = glow;
      ctx.beginPath();
      ctx.arc(cx, cy, cssSize * 0.56, 0, Math.PI * 2);
      ctx.fill();

      for (var r = 0; r < 3; r++) {
        var ringRadius = cssSize * (0.23 + r * 0.09 + Math.sin(t * 1.9 + r) * 0.008);
        ctx.strokeStyle = active
          ? rgba("ff3030", "2f7eff", (pulse + r * 0.28) % 1, 0.14)
          : "rgba(255,255,255,.12)";
        ctx.lineWidth = 0.7;
        ctx.beginPath();
        for (var j = 0; j <= 72; j++) {
          var a = (j / 72) * Math.PI * 2;
          var warp = Math.sin(a * (3 + r) + t * (2.1 + r * 0.4)) * cssSize * 0.014;
          warp += Math.sin(a * (7 + r) - t * (1.4 + r * 0.3)) * cssSize * 0.011;
          var px = cx + Math.cos(a) * (ringRadius + warp);
          var py = cy + Math.sin(a) * (ringRadius + warp);
          if (j === 0) ctx.moveTo(px, py);
          else ctx.lineTo(px, py);
        }
        ctx.closePath();
        ctx.stroke();
      }

      dots.forEach(function (dot, i) {
        var angle = dot.angle + t * dot.speed + Math.sin(t * 1.5 + dot.wobble) * 0.13;
        var lumpy = Math.sin(angle * dot.lobe + t * 2.2 + dot.wobble);
        lumpy += 0.6 * Math.sin(angle * (dot.lobe + 4) - t * 1.7);
        var escape = Math.pow(clamp(Math.sin(t * 2.8 + i * 0.21), 0, 1), 7) * cssSize * 0.095;
        var radius = dot.radius + lumpy * cssSize * 0.026 + escape;
        var squash = 1 + Math.sin(t * 1.3 + angle * 2.2) * 0.11;
        var x = cx + Math.cos(angle) * radius * squash;
        var y = cy + Math.sin(angle) * radius * (2 - squash);
        var alpha = 0.46 + 0.5 * Math.pow(Math.sin(t * 3.1 + i * 0.73), 2);
        var twinkle = 0.76 + 0.55 * Math.pow(Math.sin(t * 7.8 + i), 8);

        ctx.globalAlpha = clamp(alpha, 0.25, 0.98);
        ctx.fillStyle = active
          ? rgba("ff2c2c", "2d7fff", (pulse + i * 0.013) % 1, 1)
          : "rgb(255,255,255)";
        ctx.beginPath();
        ctx.arc(x, y, dot.size * twinkle, 0, Math.PI * 2);
        ctx.fill();

        if (i % 29 === 0) {
          ctx.globalAlpha = active ? 0.2 : 0.12;
          ctx.strokeStyle = active
            ? rgba("ff3030", "2f7eff", (pulse + i * 0.02) % 1, 1)
            : "rgb(255,255,255)";
          ctx.lineWidth = 0.45;
          ctx.beginPath();
          ctx.moveTo(cx + Math.cos(angle) * cssSize * 0.14, cy + Math.sin(angle) * cssSize * 0.14);
          ctx.lineTo(x, y);
          ctx.stroke();
        }
      });

      ctx.globalAlpha = 1;
      drawLightning(cx, cy, t, active, pulse);

      var core = ctx.createRadialGradient(cx, cy, 0, cx, cy, cssSize * 0.12);
      if (active) {
        core.addColorStop(0, rgba("ffffff", "d8e8ff", pulse, 0.96));
        core.addColorStop(0.42, rgba("ff3030", "2f7eff", pulse, 0.42));
      } else {
        core.addColorStop(0, "rgba(255,255,255,.96)");
        core.addColorStop(0.46, "rgba(255,255,255,.35)");
      }
      core.addColorStop(1, "rgba(255,255,255,0)");
      ctx.fillStyle = core;
      ctx.beginPath();
      ctx.arc(cx, cy, cssSize * 0.13, 0, Math.PI * 2);
      ctx.fill();

      requestAnimationFrame(frame);
    }

    requestAnimationFrame(frame);
  }

  function init() {
    var canvases = document.querySelectorAll("canvas[data-ck-ai-orb]");
    canvases.forEach(function (canvas, index) {
      setupOrb(canvas, 37 + index * 97);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
