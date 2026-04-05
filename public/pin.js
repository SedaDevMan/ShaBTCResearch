/* shabtc PIN lock — include in every page before </body> */
(function () {
  'use strict';

  const HASH = '4a44dc15fbcf46399506571fcca9b513ffe36c68'; // SHA1 of "2791"
  const SESSION_KEY = 'shabtc_unlocked';

  // Already unlocked this session?
  if (sessionStorage.getItem(SESSION_KEY) === '1') return;

  // ── Build overlay ──────────────────────────────────────────────────────────
  const overlay = document.createElement('div');
  overlay.id = 'pin-overlay';
  overlay.innerHTML = `
    <div id="pin-box">
      <div id="pin-title">shabtc</div>
      <div id="pin-sub">Enter access code</div>
      <div id="pin-dots"></div>
      <div id="pin-err"></div>
      <div id="pin-grid">
        ${[1,2,3,4,5,6,7,8,9,'',0,'⌫'].map(k => `<button class="pk" data-k="${k}">${k}</button>`).join('')}
      </div>
    </div>`;

  const style = document.createElement('style');
  style.textContent = `
    #pin-overlay {
      position: fixed; inset: 0; z-index: 99999;
      background: #060d1b;
      display: flex; align-items: center; justify-content: center;
    }
    #pin-box {
      display: flex; flex-direction: column; align-items: center; gap: 1rem;
      padding: 2.5rem 2rem;
      background: #0d1829;
      border: 1px solid #1a2744;
      border-radius: 16px;
      width: 280px;
    }
    #pin-title {
      font-family: system-ui, sans-serif;
      font-size: 1.4rem; font-weight: 700; color: #3b82f6; letter-spacing: .08em;
    }
    #pin-sub {
      font-family: system-ui, sans-serif;
      font-size: 0.82rem; color: #475569;
    }
    #pin-dots {
      display: flex; gap: .6rem; min-height: 14px; align-items: center; justify-content: center;
    }
    .pin-dot {
      width: 10px; height: 10px; border-radius: 50%;
      background: #3b82f6;
      animation: dotPop .15s ease;
    }
    @keyframes dotPop { from { transform: scale(0); } to { transform: scale(1); } }
    #pin-err {
      font-family: system-ui, sans-serif;
      font-size: 0.78rem; color: #ef4444;
      min-height: 1.1em; text-align: center;
    }
    #pin-grid {
      display: grid; grid-template-columns: repeat(3, 72px); gap: .5rem;
    }
    .pk {
      height: 56px;
      background: #1e293b; border: 1px solid #1a2744; border-radius: 10px;
      color: #e2e8f0; font-size: 1.2rem; font-weight: 600;
      cursor: pointer; transition: background .1s;
      font-family: system-ui, sans-serif;
    }
    .pk:hover { background: #263347; }
    .pk:active { background: #2d3f5a; }
    .pk[data-k=""] { visibility: hidden; cursor: default; }
    .pk[data-k="⌫"] { font-size: 1rem; color: #64748b; }
    #pin-overlay.shake #pin-box {
      animation: shake .35s ease;
    }
    @keyframes shake {
      0%,100%{ transform:translateX(0); }
      20%{ transform:translateX(-8px); }
      40%{ transform:translateX(8px); }
      60%{ transform:translateX(-6px); }
      80%{ transform:translateX(4px); }
    }
  `;

  document.head.appendChild(style);
  document.body.appendChild(overlay);
  document.body.style.overflow = 'hidden';

  // ── State ──────────────────────────────────────────────────────────────────
  let input = '';

  function sha1(str) {
    // Tiny SHA1 for browser — no external deps
    function rotl(n,s){ return (n<<s)|(n>>>(32-s)); }
    const msg = unescape(encodeURIComponent(str));
    const bytes = Array.from(msg).map(c=>c.charCodeAt(0));
    bytes.push(0x80);
    while (bytes.length % 64 !== 56) bytes.push(0);
    const len = (msg.length * 8);
    for (let i=7;i>=0;i--) bytes.push((len / Math.pow(2, i*8)) & 0xff);
    let [h0,h1,h2,h3,h4] = [0x67452301,0xEFCDAB89,0x98BADCFE,0x10325476,0xC3D2E1F0];
    for (let i=0;i<bytes.length;i+=64){
      const w=[];
      for(let j=0;j<16;j++) w[j]=(bytes[i+j*4]<<24)|(bytes[i+j*4+1]<<16)|(bytes[i+j*4+2]<<8)|bytes[i+j*4+3];
      for(let j=16;j<80;j++) w[j]=rotl(w[j-3]^w[j-8]^w[j-14]^w[j-16],1);
      let [a,b,c,d,e]=[h0,h1,h2,h3,h4];
      for(let j=0;j<80;j++){
        let f,k;
        if(j<20){f=(b&c)|((~b)&d);k=0x5A827999;}
        else if(j<40){f=b^c^d;k=0x6ED9EBA1;}
        else if(j<60){f=(b&c)|(b&d)|(c&d);k=0x8F1BBCDC;}
        else{f=b^c^d;k=0xCA62C1D6;}
        const tmp=(rotl(a,5)+f+e+k+w[j])>>>0;
        e=d;d=c;c=rotl(b,30);b=a;a=tmp;
      }
      h0=(h0+a)>>>0;h1=(h1+b)>>>0;h2=(h2+c)>>>0;h3=(h3+d)>>>0;h4=(h4+e)>>>0;
    }
    return [h0,h1,h2,h3,h4].map(h=>h.toString(16).padStart(8,'0')).join('');
  }

  function renderDots() {
    document.getElementById('pin-dots').innerHTML =
      Array.from({length: input.length}, () => '<span class="pin-dot"></span>').join('');
  }

  function setErr(msg) {
    document.getElementById('pin-err').textContent = msg;
  }

  function tryUnlock() {
    if (sha1(input) === HASH) {
      sessionStorage.setItem(SESSION_KEY, '1');
      overlay.style.transition = 'opacity .3s';
      overlay.style.opacity = '0';
      setTimeout(() => { overlay.remove(); style.remove(); document.body.style.overflow = ''; }, 300);
    } else {
      overlay.classList.add('shake');
      setErr('Incorrect');
      setTimeout(() => { overlay.classList.remove('shake'); setErr(''); }, 600);
      input = '';
      renderDots();
    }
  }

  function press(k) {
    if (k === '⌫') {
      input = input.slice(0, -1);
      setErr('');
      renderDots();
      return;
    }
    if (k === '') return;
    if (input.length >= 12) return; // silent max — doesn't hint at real length
    input += String(k);
    renderDots();
    // Auto-submit after each keypress attempt: try if hash matches
    // (works for any length — user presses digits until it unlocks)
    if (sha1(input) === HASH) tryUnlock();
    // Don't auto-fail — let user keep typing or backspace
  }

  overlay.querySelectorAll('.pk').forEach(btn => {
    btn.addEventListener('click', () => press(btn.dataset.k));
  });

  // Keyboard support
  document.addEventListener('keydown', e => {
    if (!document.getElementById('pin-overlay')) return;
    if (e.key >= '0' && e.key <= '9') press(e.key);
    if (e.key === 'Backspace') press('⌫');
    if (e.key === 'Enter') tryUnlock();
  });
})();
