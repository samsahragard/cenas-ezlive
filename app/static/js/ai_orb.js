(function () {
  "use strict";

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

    var seed = seedBase || 37;
    function rnd() {
      seed = (seed * 1664525 + 1013904223) >>> 0;
      return seed / 4294967296;
    }

    var colors = ["#fffaf0", "#f1d47a", "#d7493f", "#5bcac5", "#8aaaff"];
    var dots = [];
    for (var i = 0; i < 170; i++) {
      dots.push({
        angle: rnd() * Math.PI * 2,
        radius: Math.pow(rnd(), 0.58) * (cssSize * 0.43),
        size: 0.8 + rnd() * 2,
        color: colors[Math.floor(rnd() * colors.length)],
        speed: 0.25 + rnd() * 1.2
      });
    }

    function frame(t) {
      t *= 0.001;
      ctx.clearRect(0, 0, cssSize, cssSize);
      var cx = cssSize / 2;
      var cy = cssSize / 2;

      var glow = ctx.createRadialGradient(cx, cy, 3, cx, cy, cssSize * 0.54);
      glow.addColorStop(0, "rgba(255,246,210,.34)");
      glow.addColorStop(0.45, "rgba(216,179,95,.18)");
      glow.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = glow;
      ctx.beginPath();
      ctx.arc(cx, cy, cssSize * 0.54, 0, Math.PI * 2);
      ctx.fill();

      dots.forEach(function (dot, i) {
        var angle = dot.angle + t * dot.speed + Math.sin(t + i) * 0.035;
        var radius = dot.radius + Math.sin(t * 1.4 + i * 0.7) * 2.3;
        var x = cx + Math.cos(angle) * radius;
        var y = cy + Math.sin(angle) * radius;
        ctx.globalAlpha = 0.52 + 0.38 * Math.sin(t * 2 + i);
        ctx.fillStyle = dot.color;
        ctx.beginPath();
        ctx.arc(x, y, dot.size, 0, Math.PI * 2);
        ctx.fill();
      });

      ctx.globalAlpha = 0.95;
      ctx.fillStyle = "#fff4ba";
      ctx.beginPath();
      ctx.arc(cx, cy, 4, 0, Math.PI * 2);
      ctx.fill();
      ctx.globalAlpha = 1;
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
