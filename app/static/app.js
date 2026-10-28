(function () {
  "use strict";

  function renderMath(root) {
    if (!window.katex) return;
    (root || document).querySelectorAll(".math").forEach(function (el) {
      if (el.dataset.done) return;
      el.dataset.done = "1";
      try {
        window.katex.render(el.textContent, el, {
          displayMode: el.classList.contains("block"),
          throwOnError: false,
        });
      } catch (e) { /* оставляем исходный текст формулы */ }
    });
  }

  function highlight(root) {
    if (!window.hljs) return;
    (root || document).querySelectorAll("pre code").forEach(function (b) {
      if (b.dataset.highlighted) return;
      window.hljs.highlightElement(b);
    });
  }

  function isTyping(e) {
    var t = e.target;
    return t && (t.tagName === "TEXTAREA" || (t.tagName === "INPUT" && t.type === "text"));
  }

  // ---------- запись голоса: WAV 16 кГц моно прямо в браузере ----------

  function Recorder() {
    var ctx, stream, source, proc, chunks = [], inRate = 16000, startedAt = 0;

    this.start = function () {
      return navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } })
        .then(function (s) {
          stream = s;
          var AC = window.AudioContext || window.webkitAudioContext;
          ctx = new AC();
          inRate = ctx.sampleRate;
          source = ctx.createMediaStreamSource(stream);
          proc = ctx.createScriptProcessor(4096, 1, 1);
          chunks = [];
          proc.onaudioprocess = function (e) { chunks.push(new Float32Array(e.inputBuffer.getChannelData(0))); };
          source.connect(proc);
          proc.connect(ctx.destination);
          startedAt = performance.now();
        });
    };

    this.seconds = function () { return (performance.now() - startedAt) / 1000; };

    this.stop = function () {
      try { proc.disconnect(); source.disconnect(); } catch (e) { /* уже отключено */ }
      stream.getTracks().forEach(function (t) { t.stop(); });
      var rate = inRate;
      return ctx.close().then(function () { return encodeWav(chunks, rate); });
    };
  }

  function encodeWav(chunks, inRate) {
    var total = chunks.reduce(function (n, c) { return n + c.length; }, 0);
    var data = new Float32Array(total), off = 0;
    chunks.forEach(function (c) { data.set(c, off); off += c.length; });
    var outRate = 16000, ratio = inRate / outRate, outLen = Math.floor(total / ratio);
    var pcm = new Int16Array(outLen);
    for (var i = 0; i < outLen; i++) {
      // усреднение по окну — простая защита от наложения частот при понижении частоты дискретизации
      var a = Math.floor(i * ratio), b = Math.min(total, Math.max(a + 1, Math.floor((i + 1) * ratio))), sum = 0;
      for (var j = a; j < b; j++) sum += data[j];
      var v = Math.max(-1, Math.min(1, sum / (b - a)));
      pcm[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
    }
    var buf = new ArrayBuffer(44 + pcm.length * 2), dv = new DataView(buf);
    function str(o, t) { for (var k = 0; k < t.length; k++) dv.setUint8(o + k, t.charCodeAt(k)); }
    str(0, "RIFF"); dv.setUint32(4, 36 + pcm.length * 2, true); str(8, "WAVE"); str(12, "fmt ");
    dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
    dv.setUint32(24, outRate, true); dv.setUint32(28, outRate * 2, true); dv.setUint16(32, 2, true); dv.setUint16(34, 16, true);
    str(36, "data"); dv.setUint32(40, pcm.length * 2, true);
    new Int16Array(buf, 44).set(pcm);
    return new Blob([buf], { type: "audio/wav" });
  }

  // ---------- страница вопроса ----------

  function initQuestion() {
    var form = document.getElementById("qform");
    if (!form) return;
    var t0 = performance.now();
    form.addEventListener("submit", function () {
      form.elements.time_ms.value = Math.round(performance.now() - t0);
      var list = document.getElementById("order-list");
      if (list) {  // порядок элементов уходит скрытыми полями в текущей последовательности
        form.querySelectorAll('input[name="order"]').forEach(function (n) { n.remove(); });
        list.querySelectorAll("li").forEach(function (li) {
          var h = document.createElement("input");
          h.type = "hidden"; h.name = "order"; h.value = li.dataset.i;
          form.appendChild(h);
        });
      }
    });

    // --- порядок: кнопки ↑ ↓ ---
    var orderList = document.getElementById("order-list");
    if (orderList) {
      orderList.addEventListener("click", function (e) {
        var btn = e.target.closest("button[data-move]");
        if (!btn) return;
        var li = btn.closest("li"), dir = Number(btn.dataset.move);
        if (dir < 0 && li.previousElementSibling) orderList.insertBefore(li, li.previousElementSibling);
        if (dir > 0 && li.nextElementSibling) orderList.insertBefore(li.nextElementSibling, li);
        btn.focus();
      });
    }

    var revealBtn = document.getElementById("reveal");
    var box = document.getElementById("reveal-box");
    var checkBtn = document.getElementById("check-btn");
    var checkBox = document.getElementById("check-box");
    var micBtn = document.getElementById("mic");
    var micStatus = document.getElementById("mic-status");
    var textarea = document.getElementById("answer-text");
    var hint = document.getElementById("hint");
    var revealed = false;
    var opts = Array.prototype.slice.call(form.querySelectorAll('input[name="choice"]'));
    var rubric = Array.prototype.slice.call(form.querySelectorAll('input[name="rubric"]'));

    function visibleRateButtons() {
      return Array.prototype.slice.call(form.querySelectorAll(".rate button")).filter(function (b) { return b.offsetParent !== null; });
    }

    function doReveal() {
      if (!box || revealed) return;
      revealed = true;
      box.hidden = false;
      if (revealBtn) revealBtn.hidden = true;
      if (checkBtn) checkBtn.hidden = true;
      if (micBtn) micBtn.hidden = true;
      if (checkBox) checkBox.hidden = true;
      form.elements.check_id && (form.elements.check_id.value = "");
      box.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
    if (revealBtn) revealBtn.addEventListener("click", doReveal);

    function suggest() {
      if (!rubric.length) return;
      var f = rubric.filter(function (b) { return b.checked; }).length / rubric.length;
      var r = f >= 0.6 ? 3 : f >= 0.35 ? 2 : 1;  // те же пороги, что на сервере (config.RATING_*_FROM)
      form.querySelectorAll("#reveal-box .rate button").forEach(function (b) { b.classList.toggle("suggested", Number(b.value) === r); });
    }
    rubric.forEach(function (b) { b.addEventListener("change", suggest); });

    // --- голос: запись → распознавание → текст в поле ---
    var rec = null, tick = null, MAX_SECONDS = 600;

    function setStatus(msg, bad) {
      if (!micStatus) return;
      micStatus.textContent = msg || "";
      micStatus.style.color = bad ? "var(--bad)" : "";
    }

    function stopRecording() {
      if (!rec) return;
      var r = rec; rec = null;
      clearInterval(tick);
      micBtn.classList.remove("recording");
      micBtn.textContent = "🎙 Записать голосом";
      micBtn.disabled = true;
      setStatus("Распознаю речь…");
      r.stop().then(function (blob) {
        return fetch("/api/transcribe?sid=" + encodeURIComponent(form.dataset.sid) + "&qid=" + encodeURIComponent(form.dataset.qid),
          { method: "POST", headers: { "Content-Type": "audio/wav" }, body: blob });
      }).then(function (resp) { return resp.json(); }).then(function (data) {
        if (!data.ok) { setStatus(data.error || "Ошибка распознавания", true); return; }
        var cur = textarea.value.trim();
        textarea.value = cur ? cur + "\n" + data.text : data.text;
        setStatus("Готово (" + Math.round(data.seconds || 0) + " с). Проверь текст и жми «Проверить ответ».");
        textarea.focus();
      }).catch(function () {
        setStatus("Не удалось отправить запись. Проверь соединение.", true);
      }).then(function () { micBtn.disabled = false; });
    }

    if (micBtn) {
      micBtn.addEventListener("click", function () {
        if (rec) { stopRecording(); return; }
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
          setStatus("Браузер не даёт доступ к микрофону (нужен localhost или HTTPS).", true);
          return;
        }
        var r = new Recorder();
        setStatus("Жду разрешения на микрофон…");
        r.start().then(function () {
          rec = r;
          micBtn.classList.add("recording");
          micBtn.textContent = "Стоп";
          setStatus("Идёт запись…");
          tick = setInterval(function () {
            var s = Math.round(r.seconds());
            setStatus("Идёт запись… " + Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"));
            if (s >= MAX_SECONDS) stopRecording();
          }, 500);
        }).catch(function () {
          setStatus("Нет доступа к микрофону. Разреши его в настройках сайта.", true);
        });
      });
    }

    // --- проверка ответа моделью ---
    function doCheck() {
      if (!checkBtn || checkBtn.disabled || revealed) return;
      if (rec) stopRecording();
      var text = textarea.value.trim();
      if (text.length < 3) { setStatus("Сначала продиктуй или напиши ответ.", true); return; }
      checkBtn.disabled = true;
      var label = checkBtn.textContent;
      checkBtn.textContent = "Проверяю…";
      setStatus("");
      var body = new URLSearchParams({ qid: form.dataset.qid, text: text });
      fetch("/s/" + encodeURIComponent(form.dataset.sid) + "/check", { method: "POST", body: body })
        .then(function (resp) { return resp.json(); })
        .then(function (data) {
          if (!data.ok) { setStatus(data.error || "Ошибка проверки", true); return; }
          checkBox.innerHTML = data.html;
          checkBox.hidden = false;
          form.elements.check_id.value = data.check_id;
          renderMath(checkBox);
          highlight(checkBox);
          if (revealBtn) revealBtn.hidden = true;
          if (hint) hint.hidden = true;
          label = "Проверить ещё раз";
          checkBox.scrollIntoView({ block: "nearest", behavior: "smooth" });
        })
        .catch(function () { setStatus("Не удалось выполнить проверку. Проверь соединение.", true); })
        .then(function () {
          checkBtn.disabled = false;
          checkBtn.textContent = label;
        });
    }
    if (checkBtn) checkBtn.addEventListener("click", doCheck);

    document.addEventListener("keydown", function (e) {
      if (e.altKey || e.metaKey) return;
      if (opts.length) {
        if (/^[1-9]$/.test(e.key) && !e.ctrlKey && !isTyping(e)) {
          var o = opts[Number(e.key) - 1];
          if (o) { o.checked = o.type === "radio" ? true : !o.checked; e.preventDefault(); }
        } else if (e.key === "Enter" && opts.some(function (o) { return o.checked; })) {
          e.preventDefault();
          form.requestSubmit();
        }
        return;
      }
      if (!box) return;  // match / order: стандартное поведение страницы
      var rateBtns = visibleRateButtons();
      if (rateBtns.length && /^[1-4]$/.test(e.key) && !e.ctrlKey && !isTyping(e)) {
        e.preventDefault();
        var btn = rateBtns[Number(e.key) - 1];
        if (btn) btn.click();
        return;
      }
      if (revealed) return;
      if (checkBtn && e.key === "Enter" && e.ctrlKey) { e.preventDefault(); doCheck(); return; }
      var fromField = e.target && (e.target.tagName === "INPUT" || e.target.tagName === "BUTTON" || e.target.tagName === "SELECT");
      if ((e.key === "Enter" && e.ctrlKey) || ((e.key === "Enter" || e.key === " ") && !isTyping(e) && !fromField)) {
        e.preventDefault();
        if (!checkBtn || e.ctrlKey) doReveal();
      }
    });
  }

  // ---------- настройки ----------

  function initSettings() {
    var form = document.getElementById("srs-form");
    if (form) {
      form.querySelectorAll('input[name="preset"]').forEach(function (r) {
        r.addEventListener("change", function () {
          document.getElementById("srs-retention").value = r.dataset.retention;
          document.getElementById("srs-cap").value = r.dataset.cap;
        });
      });
    }
    var btn = document.getElementById("llm-test"), out = document.getElementById("llm-test-out");
    if (btn) {
      btn.addEventListener("click", function () {
        btn.disabled = true; out.hidden = false; out.textContent = "Проверяю…";
        fetch("/settings/llm/test", { method: "POST" }).then(function (r) { return r.json(); }).then(function (d) {
          function line(name, x) {
            if (!x) return name + ": не проверялось";
            return name + " (" + x.model + "): " + (x.ok ? "работает, стоимость проверки $" + (x.cost || 0).toFixed(5) : "ошибка — " + x.error);
          }
          out.textContent = d.error ? d.error : line("Распознавание речи", d.stt) + "\n" + line("Проверка ответа", d.grader);
        }).catch(function () { out.textContent = "Не удалось выполнить проверку."; }).then(function () { btn.disabled = false; });
      });
    }
  }

  function initCodeArea() {
    // Tab в поле кода вставляет 4 пробела, а не уводит фокус
    document.querySelectorAll("textarea.mono").forEach(function (ta) {
      ta.addEventListener("keydown", function (e) {
        if (e.key !== "Tab" || e.ctrlKey || e.altKey || e.metaKey) return;
        e.preventDefault();
        var s = ta.selectionStart;
        ta.setRangeText("    ", s, ta.selectionEnd, "end");
      });
    });
  }

  function initFeedback() {
    var next = document.getElementById("next");
    if (!next) return;
    document.addEventListener("keydown", function (e) {
      if ((e.key === "Enter" || e.key === " ") && !isTyping(e) && !e.target.closest("summary, button, a")) {
        e.preventDefault();
        window.location.href = next.href;
      }
    });
  }

  function initCopy() {
    document.querySelectorAll("[data-copy]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var src = document.getElementById(btn.dataset.copy);
        if (!src) return;
        navigator.clipboard.writeText(src.value || src.textContent).then(function () {
          var old = btn.textContent;
          btn.textContent = "Скопировано ✓";
          setTimeout(function () { btn.textContent = old; }, 1500);
        });
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    renderMath();
    highlight();
    initQuestion();
    initSettings();
    initCodeArea();
    initFeedback();
    initCopy();
  });
})();
