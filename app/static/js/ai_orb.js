(function () {
  "use strict";

  var WHITE = [255, 255, 255];
  var BOLT_RED = [255, 64, 48];
  var BOLT_BLUE = [70, 150, 255];

  function mixColor(a, b, k) {
    return [
      a[0] + (b[0] - a[0]) * k,
      a[1] + (b[1] - a[1]) * k,
      a[2] + (b[2] - a[2]) * k
    ];
  }

  function rgb(c) {
    return "rgb(" + (c[0] | 0) + "," + (c[1] | 0) + "," + (c[2] | 0) + ")";
  }

  function isAssistantSurface() {
    var path = window.location.pathname || "";
    var params = new URLSearchParams(window.location.search || "");
    return path.indexOf("/assistant") !== -1 ||
      path.indexOf("/sam/chat") !== -1 ||
      params.get("tab") === "cena";
  }

  function numberAttr(el, name, fallback) {
    var raw = parseInt(el.getAttribute(name) || "", 10);
    return Number.isFinite(raw) && raw > 0 ? raw : fallback;
  }

  function makeStack(canvas, width, height) {
    var stack = document.createElement("span");
    stack.className = "ck-ai-orb-stack";
    if (canvas.className) {
      stack.className += " " + canvas.className;
    }
    stack.style.position = "relative";
    stack.style.display = "block";
    stack.style.width = width + "px";
    stack.style.height = height + "px";
    stack.style.pointerEvents = "none";
    stack.style.overflow = "visible";

    canvas.parentNode.insertBefore(stack, canvas);
    stack.appendChild(canvas);

    canvas.removeAttribute("class");
    canvas.style.position = "absolute";
    canvas.style.inset = "0";
    canvas.style.width = "100%";
    canvas.style.height = "100%";
    canvas.style.display = "block";
    canvas.style.pointerEvents = "none";

    var bolts = document.createElement("canvas");
    bolts.className = "ck-ai-orb-bolts";
    bolts.setAttribute("aria-hidden", "true");
    bolts.style.position = "absolute";
    bolts.style.inset = "0";
    bolts.style.width = "100%";
    bolts.style.height = "100%";
    bolts.style.display = "block";
    bolts.style.pointerEvents = "none";
    stack.appendChild(bolts);

    return { stack: stack, dots: canvas, bolts: bolts };
  }

  function setupOrb(canvas, seedBase) {
    if (!canvas || canvas.__ckAiOrbReady) return;
    canvas.__ckAiOrbReady = true;

    var baseSize = numberAttr(canvas, "data-size", 78);
    var cssW = numberAttr(canvas, "data-width", baseSize);
    var cssH = numberAttr(canvas, "data-height", baseSize);
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    var parts = makeStack(canvas, cssW, cssH);
    var cvDots = parts.dots;
    var cvBolt = parts.bolts;
    var stack = parts.stack;

    cvDots.width = Math.round(cssW * dpr);
    cvDots.height = Math.round(cssH * dpr);
    cvBolt.width = Math.round(cssW * dpr);
    cvBolt.height = Math.round(cssH * dpr);

    var clickTarget = stack.closest("a, button");
    var clickedUntil = 0;
    var longPressTimer = null;
    var longPressHandled = false;
    var longPressPointerId = null;

    function clearLongPress() {
      if (longPressTimer) {
        clearTimeout(longPressTimer);
        longPressTimer = null;
      }
      longPressPointerId = null;
    }

    function dispatchLongPress(sourceEvent) {
      longPressTimer = null;
      clickedUntil = performance.now() + 1800;
      makeBolt();
      makeBolt();
      var event = new CustomEvent("ck-ai-orb-longpress", {
        bubbles: true,
        cancelable: true,
        detail: {
          canvas: canvas,
          stack: stack,
          target: clickTarget || stack,
          originalEvent: sourceEvent || null
        }
      });
      stack.dispatchEvent(event);
      if (event.defaultPrevented) {
        longPressHandled = true;
        return;
      }
      try {
        if (window.sessionStorage) sessionStorage.setItem("ckAiVoiceOnLoad", "1");
      } catch (err) {}
    }

    if (clickTarget) {
      clickTarget.addEventListener("pointerdown", function (event) {
        if (event.button != null && event.button !== 0) return;
        clearLongPress();
        longPressHandled = false;
        longPressPointerId = event.pointerId;
        longPressTimer = setTimeout(function () {
          dispatchLongPress(event);
        }, 520);
      }, { passive: true });
      clickTarget.addEventListener("pointerup", clearLongPress, { passive: true });
      clickTarget.addEventListener("pointercancel", clearLongPress, { passive: true });
      clickTarget.addEventListener("pointerleave", function (event) {
        if (longPressPointerId === event.pointerId) clearLongPress();
      }, { passive: true });
      clickTarget.addEventListener("click", function (event) {
        if (longPressHandled) {
          longPressHandled = false;
          event.preventDefault();
          event.stopImmediatePropagation();
          return;
        }
        clickedUntil = performance.now() + 1800;
        makeBolt();
        makeBolt();
      });
    }

    var seed = seedBase || 37;
    function rnd() {
      seed = (seed * 1664525 + 1013904223) >>> 0;
      return seed / 4294967296;
    }

    var bctx = cvBolt.getContext("2d");
    if (!bctx) return;

    var orbOn = false;
    var act = 0;
    var t = rnd() * 100;
    var bulgeL = 0;
    var bulgeR = 0;
    var bolts = [];
    var nextStrike = 26 + rnd() * 52;

    var gl = cvDots.getContext("webgl", {
      alpha: true,
      antialias: false,
      premultipliedAlpha: false
    }) || cvDots.getContext("experimental-webgl", {
      alpha: true,
      premultipliedAlpha: false
    });

    var areaScale = Math.max(0.35, Math.min(1, (cssW * cssH) / (176 * 124)));
    var pointCount = gl ? Math.round(12000 + 38000 * areaScale) : Math.round(950 + 1650 * areaScale);
    var glProg = null;
    var uT;
    var uAct;
    var uBL;
    var uBR;
    var uDrift;

    function shader(type, src) {
      var s = gl.createShader(type);
      gl.shaderSource(s, src);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) return null;
      return s;
    }

    if (gl) {
      var vs = "attribute vec3 aPos;attribute vec2 aSP;" +
        "uniform float uT,uAct,uBL,uBR;uniform vec2 uDrift,uScale;varying vec4 vCol;" +
        "void main(){float p=aSP.y;" +
        "float m=1.0+0.11*sin(3.0*aPos.x+uT*1.5+p)+0.09*sin(4.0*aPos.y-uT*1.0)+0.07*sin(5.0*aPos.z+uT*1.8);" +
        "vec3 v=aPos*m;" +
        "float ry=sin(uT*0.3)*0.3;float rxa=uT*0.7;" +
        "float cy=cos(ry),sy=sin(ry);" +
        "float x1=v.x*cy+v.z*sy;float z1=-v.x*sy+v.z*cy;" +
        "float cx=cos(rxa),sx=sin(rxa);" +
        "float y1=v.y*cx-z1*sx;float z2=v.y*sx+z1*cx;" +
        "float b=mix(uBL,uBR,step(0.0,x1));x1*=1.0+b*0.5*abs(x1);" +
        "float depth=(z2+1.4)/2.5;" +
        "gl_Position=vec4(vec2(x1,y1)*uScale+uDrift,0.0,1.0);" +
        "gl_PointSize=(0.5+depth*0.9)*aSP.x;" +
        "float cyc=0.5+0.5*sin(uT*3.0+p*0.5);" +
        "vec3 red=vec3(0.902,0.251,0.227);vec3 blue=vec3(0.282,0.549,0.933);" +
        "vec3 col=mix(vec3(1.0),mix(red,blue,cyc),uAct);" +
        "vCol=vec4(col,0.10+depth*0.24);}";
      var fs = "precision mediump float;varying vec4 vCol;" +
        "void main(){vec2 d=gl_PointCoord-0.5;float r=length(d);" +
        "if(r>0.5)discard;float fall=smoothstep(0.5,0.15,r);" +
        "gl_FragColor=vec4(vCol.rgb,vCol.a*fall);}";
      var v = shader(gl.VERTEX_SHADER, vs);
      var f = shader(gl.FRAGMENT_SHADER, fs);
      if (v && f) {
        glProg = gl.createProgram();
        gl.attachShader(glProg, v);
        gl.attachShader(glProg, f);
        gl.linkProgram(glProg);
        if (!gl.getProgramParameter(glProg, gl.LINK_STATUS)) glProg = null;
      }
      if (glProg) {
        gl.useProgram(glProg);
        var buf = new Float32Array(pointCount * 5);
        for (var i = 0; i < pointCount; i++) {
          var u = rnd() * 2 - 1;
          var th = rnd() * 6.2832;
          var rr = Math.sqrt(Math.max(0, 1 - u * u));
          var rad = rnd() < 0.45 ? 1 : (0.3 + 0.7 * Math.cbrt(rnd()));
          buf[i * 5] = Math.cos(th) * rr * rad;
          buf[i * 5 + 1] = u * rad;
          buf[i * 5 + 2] = Math.sin(th) * rr * rad;
          buf[i * 5 + 3] = (0.55 + rnd() * 0.95) * dpr;
          buf[i * 5 + 4] = rnd() * 6.2832;
        }
        var vbo = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, vbo);
        gl.bufferData(gl.ARRAY_BUFFER, buf, gl.STATIC_DRAW);
        var aPos = gl.getAttribLocation(glProg, "aPos");
        var aSP = gl.getAttribLocation(glProg, "aSP");
        gl.enableVertexAttribArray(aPos);
        gl.vertexAttribPointer(aPos, 3, gl.FLOAT, false, 20, 0);
        gl.enableVertexAttribArray(aSP);
        gl.vertexAttribPointer(aSP, 2, gl.FLOAT, false, 20, 12);
        uT = gl.getUniformLocation(glProg, "uT");
        uAct = gl.getUniformLocation(glProg, "uAct");
        uBL = gl.getUniformLocation(glProg, "uBL");
        uBR = gl.getUniformLocation(glProg, "uBR");
        uDrift = gl.getUniformLocation(glProg, "uDrift");
        gl.uniform2f(gl.getUniformLocation(glProg, "uScale"), 0.545, -0.581);
        gl.viewport(0, 0, cvDots.width, cvDots.height);
        gl.enable(gl.BLEND);
        gl.blendFuncSeparate(gl.SRC_ALPHA, gl.ONE, gl.ONE, gl.ONE);
        gl.clearColor(0, 0, 0, 0);
      }
    }

    var fpts = null;
    var fctx = null;
    if (!glProg) {
      fctx = cvDots.getContext("2d");
      fpts = [];
      for (var j = 0; j < pointCount; j++) {
        var u2 = rnd() * 2 - 1;
        var th2 = rnd() * 6.2832;
        var rr2 = Math.sqrt(Math.max(0, 1 - u2 * u2));
        var rad2 = rnd() < 0.45 ? 1 : (0.3 + 0.7 * Math.cbrt(rnd()));
        fpts.push({
          x: Math.cos(th2) * rr2 * rad2,
          y: u2 * rad2,
          z: Math.sin(th2) * rr2 * rad2,
          s: 0.3 + rnd() * 0.4,
          p: rnd() * 6.2832
        });
      }
    }

    function makeBolt() {
      var dir = rnd() < 0.5 ? -1 : 1;
      if (dir < 0) bulgeL = Math.min(0.95, bulgeL + 0.55);
      else bulgeR = Math.min(0.95, bulgeR + 0.55);

      var cx = cvBolt.width / 2;
      var cy = cvBolt.height / 2;
      var segs = 7 + (rnd() * 3 | 0);
      var len = (cssW * (0.3 + rnd() * 0.13)) * dpr;
      var ang = (dir < 0 ? Math.PI : 0) + (rnd() - 0.5) * 0.4;
      var ux = Math.cos(ang);
      var uy = Math.sin(ang) * 0.35;
      var px = cx + (rnd() - 0.5) * cssW * 0.1 * dpr;
      var py = cy + (rnd() - 0.5) * cssH * 0.1 * dpr;
      var p = [[px, py]];

      for (var s = 1; s <= segs; s++) {
        var dist = len * s / segs;
        var jit = (rnd() - 0.5) * 2 * (4 + 9 * Math.sin(Math.PI * s / segs)) * dpr;
        p.push([px + ux * dist - uy * jit, py + uy * dist + jit]);
      }

      var bolt = { p: p, life: 1, br: null, col: rnd() < 0.5 ? BOLT_RED : BOLT_BLUE };
      if (rnd() < 0.45) {
        var k = 2 + (rnd() * 3 | 0);
        var bx = p[k][0];
        var by = p[k][1];
        var bl = len * 0.35;
        var ba = ang + (rnd() < 0.5 ? -0.8 : 0.8);
        var bp = [[bx, by]];
        for (var s2 = 1; s2 <= 3; s2++) {
          bp.push([
            bx + Math.cos(ba) * bl * s2 / 3 + (rnd() - 0.5) * 8 * dpr,
            by + Math.sin(ba) * bl * s2 / 3 * 0.5 + (rnd() - 0.5) * 8 * dpr
          ]);
        }
        bolt.br = bp;
      }
      bolts.push(bolt);
    }

    function drawPath(p) {
      bctx.beginPath();
      bctx.moveTo(p[0][0], p[0][1]);
      for (var j = 1; j < p.length; j++) bctx.lineTo(p[j][0], p[j][1]);
      bctx.stroke();
    }

    function isActive(now) {
      return isAssistantSurface() || now < clickedUntil ||
        !!(clickTarget && clickTarget.classList && clickTarget.classList.contains("active"));
    }

    function frame(now) {
      t += 0.012;
      orbOn = isActive(now);
      act += ((orbOn ? 1 : 0) - act) * 0.07;
      bulgeL *= 0.93;
      bulgeR *= 0.93;
      if (--nextStrike <= 0) {
        makeBolt();
        nextStrike = 45 + rnd() * 95;
      }

      var dx = Math.sin(t * 0.6) * 3 * dpr;
      var dy = Math.cos(t * 0.8) * 2 * dpr;

      if (glProg) {
        gl.clear(gl.COLOR_BUFFER_BIT);
        gl.uniform1f(uT, t);
        gl.uniform1f(uAct, act);
        gl.uniform1f(uBL, bulgeL);
        gl.uniform1f(uBR, bulgeR);
        gl.uniform2f(uDrift, dx / (cvDots.width / 2), -dy / (cvDots.height / 2));
        gl.drawArrays(gl.POINTS, 0, pointCount);
      } else if (fctx && fpts) {
        fctx.clearRect(0, 0, cvDots.width, cvDots.height);
        var cx = cvDots.width / 2 + dx;
        var cy = cvDots.height / 2 + dy;
        var sx = cvDots.width * 0.27;
        var sy = cvDots.height * 0.29;
        var rxa = t * 0.7;
        var rya = Math.sin(t * 0.3) * 0.3;
        var cox = Math.cos(rxa);
        var six = Math.sin(rxa);
        var coy = Math.cos(rya);
        var siy = Math.sin(rya);
        for (var i = 0; i < pointCount; i++) {
          var fp = fpts[i];
          var m = 1 + 0.11 * Math.sin(3 * fp.x + t * 1.5 + fp.p) +
            0.09 * Math.sin(4 * fp.y - t * 1.0) +
            0.07 * Math.sin(5 * fp.z + t * 1.8);
          var x = fp.x * m;
          var y = fp.y * m;
          var z = fp.z * m;
          var x1 = x * coy + z * siy;
          var z1 = -x * siy + z * coy;
          var y1 = y * cox - z1 * six;
          var z2 = y * six + z1 * cox;
          if (x1 < 0) x1 *= 1 + bulgeL * 0.5 * (-x1);
          else x1 *= 1 + bulgeR * 0.5 * x1;
          var depth = (z2 + 1.4) / 2.5;
          fctx.globalAlpha = Math.max(0.3, Math.min(1, 0.38 + depth * 0.6));
          var cyc = 0.5 + 0.5 * Math.sin(t * 3 + fp.p * 0.5);
          var col = mixColor(WHITE, mixColor([230, 64, 58], [72, 140, 238], cyc), act);
          var r2 = (0.38 + depth * 0.62) * fp.s * dpr;
          fctx.fillStyle = rgb(col);
          fctx.fillRect(cx + x1 * sx - r2, cy + y1 * sy - r2, r2 * 2, r2 * 2);
        }
        fctx.globalAlpha = 1;
      }

      bctx.clearRect(0, 0, cvBolt.width, cvBolt.height);
      bctx.save();
      bctx.globalCompositeOperation = "lighter";
      bctx.lineWidth = 1.4 * dpr;
      bctx.lineJoin = "round";
      bctx.lineCap = "round";
      for (var b = bolts.length - 1; b >= 0; b--) {
        var bo = bolts[b];
        bo.life -= 0.055;
        if (bo.life <= 0) {
          bolts.splice(b, 1);
          continue;
        }
        var bc = mixColor(WHITE, bo.col || BOLT_RED, act);
        bctx.globalAlpha = Math.min(1, bo.life * 1.4);
        bctx.strokeStyle = rgb(bc);
        bctx.shadowColor = rgb(bc);
        bctx.shadowBlur = 8 * dpr;
        drawPath(bo.p);
        if (bo.br) {
          bctx.globalAlpha *= 0.7;
          drawPath(bo.br);
        }
      }
      bctx.restore();
      bctx.globalAlpha = 1;

      requestAnimationFrame(frame);
    }

    makeBolt();
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
