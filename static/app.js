    const $ = (id) => document.getElementById(id);
    const form = $('form'), input = $('url'), go = $('go'), goLabel = $('goLabel');
    const statusEl = $('status'), result = $('result'), qlist = $('qlist');
    const dl = $('dl'), dlLabel = $('dlLabel'), dlBar = $('dlBar'), dlStatus = $('dlStatus');
    const tabs = $('tabs'), pasteBtn = $('paste'), wmRow = $('wmRow'), wmBox = $('wm');
    const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
    const compact = new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 });
    const whole = new Intl.NumberFormat('en');
    const KINDS = ['video', 'audio', 'document'];
    const DL_LABEL = { video: 'Download MP4', audio: 'Download audio', document: 'Download details (TXT)' };
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));

    let current = null;
    let kind = 'video';
    let lastUrl = '';
    let dlBusy = false;

    $('year').textContent = new Date().getFullYear();

    /* ---------- helpers ---------- */
    function fmtDuration(s) {
      if (!s) return '';
      return `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, '0')}`;
    }
    function fmtSize(bytes) {
      if (!bytes) return '';
      return bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.round(bytes / 1024)} KB`;
    }
    function setStatus(el, text, isError = false) {
      el.textContent = text;
      el.className = 'status' + (isError ? ' error' : '');
    }
    let toastTimer;
    function toast(text) {
      const t = $('toast');
      t.textContent = text;
      t.classList.add('on');
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => t.classList.remove('on'), 2800);
    }
    function setBusy(busy) {
      document.body.classList.toggle('loading', busy);
      go.disabled = busy;
      goLabel.textContent = busy ? 'Getting video...' : 'Get video';
    }

    /* ---------- live download counter ---------- */
    const countEl = $('count'), countLabel = $('countLabel'), liveEl = $('live');
    let shown = 0, target = 0, tween = 0;
    function showCount(n) {
      target = n;
      countLabel.textContent = n === 1 ? 'download' : 'downloads';
      liveEl.hidden = false;
      cancelAnimationFrame(tween);
      if (reduceMotion || shown === target) { shown = target; countEl.textContent = whole.format(shown); return; }
      const from = shown, start = performance.now(), dur = Math.min(1200, 300 + Math.abs(target - from) * 8);
      const step = (now) => {
        const p = Math.min(1, (now - start) / dur);
        shown = Math.round(from + (target - from) * (1 - Math.pow(1 - p, 3)));
        countEl.textContent = whole.format(shown);
        if (p < 1) tween = requestAnimationFrame(step);
      };
      tween = requestAnimationFrame(step);
    }
    async function refreshCount() {
      try {
        const res = await fetch('/api/stats', { cache: 'no-store' });
        if (res.ok) showCount((await res.json()).downloads || 0);
      } catch { /* keep the old number */ }
    }
    refreshCount();
    setInterval(() => { if (!document.hidden) refreshCount(); }, 8000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshCount(); });

    /* ---------- paste / clear ---------- */
    function syncPasteLabel() { pasteBtn.textContent = input.value ? 'Clear' : 'Paste'; }
    input.addEventListener('input', syncPasteLabel);
    pasteBtn.addEventListener('click', async () => {
      if (input.value) { input.value = ''; syncPasteLabel(); input.focus(); return; }
      try {
        const text = (await navigator.clipboard.readText()).trim();
        if (!text) { setStatus(statusEl, 'Your clipboard is empty. Copy a TikTok link first.', true); return; }
        input.value = text;
        syncPasteLabel();
        if (/tiktok\.com/i.test(text)) form.requestSubmit();
      } catch {
        input.focus();
        setStatus(statusEl, 'Long-press the box and choose Paste.');
      }
    });

    /* ---------- result rendering ---------- */
    function formatsOf(k) { return current.formats.filter(f => f.kind === k); }
    function pickedId() { const p = qlist.querySelector('input:checked'); return p ? p.value : ''; }

    function updateDownload() {
      const id = pickedId();
      if (!id) return;
      dl.href = `${current.download}&f=${encodeURIComponent(id)}`;
      dlLabel.textContent = DL_LABEL[kind];
    }

    function renderList() {
      qlist.replaceChildren(...formatsOf(kind).map((f, i) => {
        const label = document.createElement('label');
        label.className = 'q';
        const radio = document.createElement('input');
        Object.assign(radio, { type: 'radio', name: 'quality', value: f.id, checked: i === 0 });
        radio.addEventListener('change', updateDownload);
        const box = document.createElement('span');
        box.className = 'qbox';
        const name = document.createElement('span');
        name.textContent = f.label;
        const size = document.createElement('span');
        size.className = 'qsize';
        size.textContent = fmtSize(f.size);
        box.append(name, size);
        label.append(radio, box);
        return label;
      }));
      qlist.style.animation = 'none';
      void qlist.offsetWidth;
      qlist.style.animation = '';
      updateDownload();
    }

    function setKind(k) {
      if (dlBusy) return;
      kind = k;
      tabs.style.setProperty('--i', KINDS.indexOf(k));
      tabs.querySelectorAll('.tab').forEach(b => b.setAttribute('aria-selected', String(b.dataset.kind === k)));
      wmRow.hidden = !(k === 'video' && current.watermark);
      setStatus(dlStatus, '');
      renderList();
    }
    tabs.addEventListener('click', (e) => {
      const b = e.target.closest('.tab');
      if (b && !b.disabled) setKind(b.dataset.kind);
    });

    function render(data) {
      current = data;
      const thumb = $('thumb');
      thumb.classList.remove('loaded');
      thumb.hidden = !data.thumbnail;
      thumb.onload = () => thumb.classList.add('loaded');
      thumb.onerror = () => { thumb.hidden = true; };
      thumb.src = data.thumbnail || '';

      $('title').textContent = data.title;
      $('author').textContent = [data.author, data.handle ? '@' + data.handle : ''].filter(Boolean).join('  ');

      const stats = [
        fmtDuration(data.duration),
        data.views != null ? compact.format(data.views) + ' views' : '',
        data.likes != null ? compact.format(data.likes) + ' likes' : '',
        data.comments != null ? compact.format(data.comments) + ' comments' : '',
        data.shares != null ? compact.format(data.shares) + ' shares' : '',
        data.created ? 'Posted ' + data.created : ''
      ].filter(Boolean);
      $('stats').replaceChildren(...stats.map((t, i) => {
        const li = document.createElement('li');
        li.textContent = t;
        li.style.setProperty('--i', i);
        return li;
      }));

      tabs.querySelectorAll('.tab').forEach(b => { b.disabled = formatsOf(b.dataset.kind).length === 0; });
      setKind(formatsOf('video').length ? 'video' : (KINDS.find(k => formatsOf(k).length) || 'document'));

      result.hidden = false;
      result.classList.remove('show');
      void result.offsetWidth;
      result.classList.add('show');
      setTimeout(() => result.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'nearest' }), 120);
    }

    /* ---------- look up a video ---------- */
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const url = input.value.trim();
      if (!url || dlBusy) return;

      setBusy(true);
      result.hidden = true;
      setStatus(statusEl, 'Looking up your video...');

      try {
        const res = await fetch('/api/info', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error + (data.detail ? ` (${data.detail})` : ''));
        lastUrl = url;
        setStatus(statusEl, '');
        render(data);
      } catch (err) {
        setStatus(statusEl, err.message || 'Something went wrong. Try again.', true);
      } finally {
        setBusy(false);
      }
    });

    /* ---------- download (with or without the watermark) ---------- */
    function setDlBusy(busy, label) {
      dlBusy = busy;
      dl.classList.toggle('busy', busy);
      dl.setAttribute('aria-disabled', String(busy));
      dlBar.hidden = !busy;
      if (busy) dlLabel.textContent = label;
      else updateDownload();
    }
    function startFileDownload(href) {
      const a = document.createElement('a');
      a.href = href;
      a.download = '';
      document.body.appendChild(a);
      a.click();
      a.remove();
    }

    async function downloadWithWatermark() {
      setStatus(dlStatus, '');
      setDlBusy(true, 'Preparing...');
      dlBar.className = 'dlbar wait';
      const bar = dlBar.querySelector('i');
      try {
        const res = await fetch('/api/prepare', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url: lastUrl, f: pickedId() })
        });
        const first = await res.json();
        if (!res.ok) throw new Error(first.error || 'Could not start.');

        const deadline = Date.now() + 6 * 60 * 1000;
        for (;;) {
          await sleep(900);
          if (Date.now() > deadline) throw new Error('This is taking too long. Try again, or switch the watermark off.');
          const r = await fetch(`/api/job/${first.job}`, { cache: 'no-store' });
          const job = await r.json();
          if (!r.ok) throw new Error(job.error || 'Lost track of the download.');

          if (job.state === 'error') throw new Error(job.error || 'Could not add the watermark.');
          if (job.state === 'done') {
            dlBar.className = 'dlbar';
            bar.style.width = '100%';
            dlLabel.textContent = 'Starting download...';
            await sleep(250);
            startFileDownload(job.file);
            toast('Your download is starting...');
            setTimeout(refreshCount, 1500);
            break;
          }
          if (job.stage === 'queued') {
            dlLabel.textContent = 'Waiting in line...';
            dlBar.className = 'dlbar wait';
          } else if (job.stage === 'downloading') {
            dlLabel.textContent = 'Fetching the video...';
            dlBar.className = 'dlbar wait';
          } else {
            dlLabel.textContent = job.percent > 0 ? `Adding watermark... ${job.percent}%` : 'Adding watermark...';
            if (job.percent > 0) { dlBar.className = 'dlbar'; bar.style.width = job.percent + '%'; }
            else dlBar.className = 'dlbar wait';
          }
        }
      } catch (err) {
        setStatus(dlStatus, err.message || 'Something went wrong. Try again.', true);
      } finally {
        bar.style.width = '0';
        setDlBusy(false);
      }
    }

    dl.addEventListener('click', (e) => {
      if (dlBusy) { e.preventDefault(); return; }
      const wantsWatermark = kind === 'video' && current && current.watermark && wmBox.checked;
      if (wantsWatermark) {
        e.preventDefault();
        downloadWithWatermark();
      } else {
        toast('Your download is starting...');
        setTimeout(refreshCount, 1500);
      }
    });

    /* ---------- FAQ ---------- */
    document.querySelectorAll('.faq-q').forEach(btn => {
      btn.addEventListener('click', () => {
        const open = btn.parentElement.classList.toggle('open');
        btn.setAttribute('aria-expanded', String(open));
      });
    });

    /* ---------- scroll reveal ---------- */
    const reveals = document.querySelectorAll('.reveal');
    if ('IntersectionObserver' in window) {
      const io = new IntersectionObserver((entries) => {
        entries.forEach(en => { if (en.isIntersecting) { en.target.classList.add('in'); io.unobserve(en.target); } });
      }, { threshold: 0.15 });
      reveals.forEach(el => io.observe(el));
    } else {
      reveals.forEach(el => el.classList.add('in'));
    }

      
