(() => {
  if (window.__KPA_ENGINE_V2__) return;
  window.__KPA_ENGINE_V2__ = true;

  const PROFILE_FEED_PATH = '/rest/o/w/pc/feed/profile';
  const COUNT_RE = /^\s*([0-9][0-9.,]*)(?:\s*)(k|m|b|mil|mi|milh[aã]o|milh[oõ]es)?\s*$/i;
  const feedQueue = [];
  let armedLink = null;
  let capturedLink = null;
  let scrollTarget = null;
  const xhrMeta = new WeakMap();

  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const normalizeText = (v) => String(v ?? '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();

  function normalizeHashtag(value) {
    let text = normalizeText(value).normalize('NFKC').replace(/^#+/, '').trim();
    if (!text) return null;
    text = text.split(/\s+/)[0];
    const valid = text.match(/^[\p{L}\p{M}\p{N}_]+/u)?.[0] || '';
    return valid ? `#${valid.toLocaleLowerCase('pt-BR')}` : null;
  }

  function parseCount(value) {
    if (typeof value === 'number' && Number.isFinite(value)) return Math.max(0, Math.round(value));
    const s = normalizeText(value).toLowerCase();
    const m = s.match(COUNT_RE);
    if (!m) return null;
    let raw = m[1];
    const suffix = (m[2] || '').toLowerCase();
    if (suffix) {
      if (raw.includes(',') && raw.includes('.')) {
        const decimal = raw.lastIndexOf(',') > raw.lastIndexOf('.') ? ',' : '.';
        const thousand = decimal === ',' ? '.' : ',';
        raw = raw.split(thousand).join('').replace(decimal, '.');
      } else raw = raw.replace(',', '.');
    } else {
      raw = raw.replace(/(?<=\d)[.,](?=\d{3}(?:\D|$))/g, '').replace(',', '.');
    }
    const base = Number(raw);
    if (!Number.isFinite(base)) return null;
    let mult = 1;
    if (suffix === 'k' || suffix === 'mil') mult = 1e3;
    if (suffix === 'm' || suffix === 'mi' || suffix.startsWith('milh')) mult = 1e6;
    if (suffix === 'b') mult = 1e9;
    return Math.max(0, Math.round(base * mult));
  }

  function isProfileFeedUrl(raw) {
    try {
      const u = new URL(String(raw || ''), location.href);
      return /(^|\.)kwai\.com$/i.test(u.hostname) && u.pathname === PROFILE_FEED_PATH;
    } catch { return false; }
  }

  function bodyLooksProfile(body) {
    const s = String(body || '').toLowerCase();
    return /visionprofilephotolist|profile.*(?:photo|video|feed)|(?:photo|video).*profile/.test(s);
  }

  function safeHeaders(headers) {
    const out = [];
    try {
      for (const [name, value] of headers.entries()) {
        if (/^(?:cookie|host|content-length|origin|referer|user-agent|sec-|accept-encoding|connection)/i.test(name)) continue;
        out.push([name, value]);
      }
    } catch {}
    return out;
  }

  function pushFeed(info, text, source) {
    if (!text || typeof text !== 'string' || text.length > 15_000_000) return;
    if (!isProfileFeedUrl(info?.url)) return;
    feedQueue.push({
      source: source || 'page-hook',
      url: String(info?.url || ''),
      method: String(info?.method || 'GET'),
      headers: Array.isArray(info?.headers) ? info.headers : [],
      bodyText: String(info?.bodyText || ''),
      responseText: text,
      at: Date.now()
    });
    while (feedQueue.length > 12) feedQueue.shift();
  }

  async function requestInfoFromFetch(input, init) {
    try {
      const req = new Request(input, init);
      let bodyText = '';
      if (!/^(GET|HEAD)$/i.test(req.method)) {
        try { bodyText = await req.clone().text(); } catch {}
      }
      return {
        url: req.url,
        method: req.method || 'GET',
        headers: safeHeaders(req.headers),
        bodyText,
      };
    } catch {
      return {
        url: typeof input === 'string' ? input : input?.url || location.href,
        method: init?.method || 'GET',
        headers: [],
        bodyText: typeof init?.body === 'string' ? init.body : ''
      };
    }
  }

  try {
    const nativeFetch = window.fetch.bind(window);
    window.fetch = function(input, init) {
      const infoPromise = requestInfoFromFetch(input, init);
      const result = nativeFetch(input, init);
      result.then(async response => {
        try {
          const info = await infoPromise;
          if (!isProfileFeedUrl(info.url)) return;
          const clone = response.clone();
          const text = await clone.text();
          pushFeed(info, text, 'fetch-hook');
        } catch {}
      }).catch(() => {});
      return result;
    };
  } catch {}

  try {
    const proto = window.XMLHttpRequest?.prototype;
    const nativeOpen = proto?.open;
    const nativeSend = proto?.send;
    const nativeSetHeader = proto?.setRequestHeader;
    if (proto && nativeOpen && nativeSend) {
      proto.open = function(method, url, ...rest) {
        xhrMeta.set(this, { method: String(method || 'GET'), url: String(url || ''), headers: [], bodyText: '' });
        return nativeOpen.call(this, method, url, ...rest);
      };
      if (nativeSetHeader) {
        proto.setRequestHeader = function(name, value) {
          const meta = xhrMeta.get(this);
          if (meta) meta.headers.push([String(name), String(value)]);
          return nativeSetHeader.call(this, name, value);
        };
      }
      proto.send = function(body) {
        const meta = xhrMeta.get(this) || { method: 'GET', url: '', headers: [], bodyText: '' };
        if (typeof body === 'string') meta.bodyText = body;
        xhrMeta.set(this, meta);
        const onDone = () => {
          if (!isProfileFeedUrl(meta.url)) return;
          try {
            if (!this.responseType || this.responseType === 'text') {
              const text = this.responseText;
              if (text) pushFeed(meta, text, 'xhr-hook');
            } else if (this.responseType === 'json' && this.response) {
              // Fallback only. Python/CDP is preferred because JSON.stringify can lose
              // precision for very large numeric IDs.
              pushFeed(meta, JSON.stringify(this.response), 'xhr-json-fallback');
            }
          } catch {}
        };
        this.addEventListener('loadend', onDone, { once: true });
        return nativeSend.call(this, body);
      };
    }
  } catch {}

  function isVideoPageUrl(value) {
    try {
      const u = new URL(String(value), location.href);
      if (!/(^|\.)kwai\.com$/i.test(u.hostname)) return false;
      const p = u.pathname.toLowerCase();
      if (/\/(?:video|photo|short-video|shortvideo|play)\/[^/?#]+/.test(p)) return true;
      for (const key of ['photoId','photoid','videoId','videoid','contentId','contentid','postId','postid']) {
        if (u.searchParams.get(key)) return true;
      }
      return false;
    } catch { return false; }
  }

  function normalizeVideoUrl(raw) {
    try {
      if (raw == null || String(raw).trim() === '' || String(raw).trim() === '#') return null;
      const u = new URL(String(raw), location.href);
      if (!/(^|\.)kwai\.com$/i.test(u.hostname) || !isVideoPageUrl(u.href)) return null;
      u.hash = '';
      if (/\/(?:video|photo|short-video|shortvideo|play)\/[^/?#]+/i.test(u.pathname)) return `${u.origin}${u.pathname}`;
      return u.href;
    } catch { return null; }
  }

  function captureLink(kind, raw) {
    if (!armedLink) return false;
    const url = normalizeVideoUrl(raw);
    if (!url) return false;
    capturedLink = { token: armedLink.token, url, kind, at: Date.now() };
    armedLink = null;
    return true;
  }

  try {
    const originalOpen = window.open;
    window.open = function(...args) {
      if (armedLink) {
        const shouldSuppress = armedLink.suppress !== false;
        if (captureLink('window.open', args[0]) && shouldSuppress) return null;
      }
      return originalOpen.apply(window, args);
    };

    const nativePush = history.pushState.bind(history);
    history.pushState = function(state, unused, url) {
      if (armedLink && url != null) {
        const shouldSuppress = armedLink.suppress !== false;
        if (captureLink('history.pushState', url) && shouldSuppress) return;
      }
      return nativePush(state, unused, url);
    };

    const nativeReplace = history.replaceState.bind(history);
    history.replaceState = function(state, unused, url) {
      if (armedLink && url != null) {
        const shouldSuppress = armedLink.suppress !== false;
        if (captureLink('history.replaceState', url) && shouldSuppress) return;
      }
      return nativeReplace(state, unused, url);
    };

    document.addEventListener('click', (event) => {
      if (!armedLink) return;
      const target = event.target instanceof Element ? event.target : null;
      const a = target?.closest?.('a[href]');
      if (!a) return;
      const href = a.href || a.getAttribute('href');
      const shouldSuppress = armedLink.suppress !== false;
      if (captureLink('anchor', href) && shouldSuppress) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
    }, true);
  } catch {}

  function rectOf(el) { try { return el?.getBoundingClientRect?.() || null; } catch { return null; } }
  function isVisible(el) {
    const r = rectOf(el);
    if (!r || r.width < 1 || r.height < 1) return false;
    try {
      const s = getComputedStyle(el);
      return s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity || 1) !== 0;
    } catch { return true; }
  }
  function hasSameTextChild(el, text) {
    try { return [...el.children].some(c => normalizeText(c.innerText || c.textContent) === text); } catch { return false; }
  }

  function metricElements() {
    const out = [];
    for (const el of document.querySelectorAll('span, div, p, strong, small')) {
      if (!isVisible(el)) continue;
      const text = normalizeText(el.innerText || el.textContent);
      if (!COUNT_RE.test(text)) continue;
      if (hasSameTextChild(el, text)) continue;
      const r = rectOf(el);
      if (!r || r.width > 180 || r.height > 55) continue;
      const value = parseCount(text);
      if (!Number.isFinite(value)) continue;
      out.push({ el, text, value, rect: r });
    }
    return out;
  }

  function mediaRectInside(ancestor, metricRect) {
    for (const node of ancestor.querySelectorAll?.('img, video, canvas') || []) {
      if (!isVisible(node)) continue;
      const r = rectOf(node);
      if (!r || r.width < 85 || r.height < 85) continue;
      const x = metricRect.left + metricRect.width / 2;
      const y = metricRect.top + metricRect.height / 2;
      if (x >= r.left - 8 && x <= r.right + 8 && y >= r.top - 8 && y <= r.bottom + 8) return { node, rect: r };
    }
    const r = rectOf(ancestor);
    if (r && r.width >= 85 && r.height >= 85) {
      try {
        const bg = getComputedStyle(ancestor).backgroundImage || '';
        if (bg && bg !== 'none') {
          const x = metricRect.left + metricRect.width / 2;
          const y = metricRect.top + metricRect.height / 2;
          if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) return { node: ancestor, rect: r, background: bg };
        }
      } catch {}
    }
    return null;
  }

  function findThumbnail(metric) {
    let node = metric.el;
    for (let depth = 0; node && node !== document.body && depth < 10; depth++, node = node.parentElement) {
      const r = rectOf(node);
      if (!r) continue;
      if (r.width < 95 || r.width > 540 || r.height < 100 || r.height > 760) continue;
      const media = mediaRectInside(node, metric.rect);
      if (media) return { element: node, rect: r, media: media.node, mediaRect: media.rect, background: media.background || null };
    }
    return null;
  }

  function findCardContainer(thumbnail) {
    const base = thumbnail.rect;
    let best = thumbnail.element;
    let node = thumbnail.element.parentElement;
    for (let depth = 0; node && node !== document.body && depth < 6; depth++, node = node.parentElement) {
      const r = rectOf(node);
      if (!r) continue;
      const widthOk = r.width >= base.width * .85 && r.width <= Math.max(base.width * 1.65, base.width + 90);
      const heightOk = r.height >= base.height * .90 && r.height <= Math.max(base.height * 2.1, base.height + 190);
      if (!widthOk || !heightOk) break;
      best = node;
    }
    return best;
  }

  // Never climb into a grid row or read a sibling video's anchor/identity.
  function ownScopes(card, thumbnail) {
    const scopes=[];
    let node=thumbnail.media || thumbnail.element;
    for(let depth=0;node && node!==document.body && depth<8;depth++,node=node.parentElement){
      const media=[...node.querySelectorAll('img,video,canvas')].filter(el=>{
        const r=rectOf(el);return r && r.width>=85 && r.height>=85;
      });
      if(media.length>1) break;
      scopes.push(node);
      if(node===card) break;
    }
    return scopes;
  }
  function extractUrl(card, thumbnail) {
    const urls=new Set();
    for(const node of ownScopes(card,thumbnail)){
      if(node.matches?.('a[href]')) {const url=normalizeVideoUrl(node.href);if(url)urls.add(url);}
      for(const a of node.querySelectorAll?.('a[href]')||[]){const url=normalizeVideoUrl(a.href);if(url)urls.add(url);}
      for(const attr of ['data-href','data-url','data-link','data-path']){
        const url=normalizeVideoUrl(node.getAttribute?.(attr));if(url)urls.add(url);
      }
    }
    return urls.size===1?[...urls][0]:null;
  }
  function extractId(card, thumbnail) {
    const ids=new Map();
    for(const node of ownScopes(card,thumbnail)){
      for(const name of ['data-photo-id','data-video-id']){
        const value=normalizeText(node.getAttribute?.(name));
        if(/^\d{12,24}$/.test(value)) ids.set(value,{field:name,value});
      }
    }
    return ids.size===1?[...ids.values()][0]:null;
  }

  function usableThumbnailUrl(raw) {
    if (!raw) return null;
    const value = String(raw).trim();
    if (!value || /^(?:data|blob):/i.test(value)) return null;
    try {
      const u = new URL(value, location.href);
      return /^https?:$/i.test(u.protocol) ? u.href : null;
    } catch { return null; }
  }
  function thumbFingerprint(raw) {
    const usable = usableThumbnailUrl(raw);
    if (!usable) return null;
    try { const u = new URL(usable); u.hash=''; u.search=''; return u.href; } catch { return usable.split(/[?#]/,1)[0]; }
  }
  function backgroundUrl(el) {
    try { return (getComputedStyle(el).backgroundImage || '').match(/url\(["']?(.*?)["']?\)/i)?.[1] || null; } catch { return null; }
  }
  function extractThumbnailUrl(thumbnail) {
    const media = thumbnail.media;
    const candidates = [];
    if (media?.tagName === 'IMG') {
      candidates.push(media.currentSrc, media.src);
      for (const attr of ['data-src','data-original','data-lazy-src','data-url']) candidates.push(media.getAttribute?.(attr));
      const srcset = media.getAttribute?.('srcset') || media.getAttribute?.('data-srcset') || '';
      for (const part of String(srcset).split(',')) candidates.push(part.trim().split(/\s+/)[0]);
    } else if (media?.tagName === 'VIDEO') candidates.push(media.poster);
    if (thumbnail.background) candidates.push(thumbnail.background.match(/url\(["']?(.*?)["']?\)/i)?.[1]);
    candidates.push(backgroundUrl(thumbnail.element));
    for (const c of candidates) { const u = usableThumbnailUrl(c); if (u) return u; }
    return null;
  }

  function extractTitle(card, metricText, thumbnail) {
    const aria = normalizeText(card.getAttribute?.('aria-label'));
    if (aria && aria !== metricText && !COUNT_RE.test(aria) && aria.length > 3) return aria.slice(0,240);
    const alt = normalizeText(thumbnail.media?.getAttribute?.('alt'));
    if (alt && alt !== metricText && !COUNT_RE.test(alt) && !/^image$/i.test(alt)) return alt.slice(0,240);
    const lines = String(card.innerText || '').split(/\n+/).map(normalizeText).filter(Boolean);
    const useful = lines.filter(line => line !== metricText && !COUNT_RE.test(line) && !/^(vídeo|video|seguir|follow|fixado|pinned)$/i.test(line) && line.length >= 4);
    return useful.length ? useful.sort((a,b)=>b.length-a.length)[0].slice(0,240) : '';
  }

  function extractHashtags(card, title='') {
    const raw = `${card?.textContent || ''} ${card?.getAttribute?.('aria-label') || ''} ${title || ''}`.normalize('NFKC');
    const out = new Set();
    for (const tag of raw.match(/#[\p{L}\p{M}\p{N}_]+/gu) || []) { const n=normalizeHashtag(tag); if(n) out.add(n); }
    return [...out];
  }

  function findClickTarget(card, thumbnail) {
    const candidates = [];
    let node = thumbnail.media || thumbnail.element;
    for (let depth=0; node && node!==document.body && depth<7; depth++,node=node.parentElement) {
      const r=rectOf(node); if(!r||r.width<40||r.height<40) continue;
      let score=0;
      if(node.tagName==='A') score+=100;
      const role=node.getAttribute?.('role'); if(role==='link') score+=80; if(role==='button') score+=70;
      if(node.hasAttribute?.('onclick')) score+=60; if(Number(node.tabIndex)>=0) score+=25;
      try{ if(getComputedStyle(node).cursor==='pointer') score+=45; }catch{}
      if(node===thumbnail.media) score+=15; if(node===thumbnail.element) score+=10; if(node===card) score+=5;
      candidates.push({node,score}); if(node===card) break;
    }
    candidates.sort((a,b)=>b.score-a.score);
    return candidates[0]?.node || thumbnail.media || thumbnail.element || card;
  }

  function extractDuration(card, thumbnail) {
    const video = thumbnail.media?.tagName === 'VIDEO' ? thumbnail.media : card.querySelector('video');
    if (Number.isFinite(video?.duration) && video.duration > 0) return video.duration;
    // Read only duration badges, not timestamps embedded in captions.
    for (const el of card.querySelectorAll('[class*="duration" i], [data-duration]')) {
      const text = normalizeText(el.textContent);
      if (!/^\d{1,3}:[0-5]\d(?::[0-5]\d)?$/.test(text)) continue;
      const seconds = text.split(':').reduce((total, part) => total * 60 + Number(part), 0);
      if (seconds > 0) return seconds;
    }
    return null;
  }

  function snapshot(metric) {
    const thumbnail=findThumbnail(metric); if(!thumbnail) return null;
    const card=findCardContainer(thumbnail);
    const url=extractUrl(card,thumbnail);
    const thumb=extractThumbnailUrl(thumbnail);
    const fp=thumbFingerprint(thumb);
    const id=extractId(card,thumbnail);
    const title=extractTitle(card,metric.text,thumbnail);
    const hashtags=extractHashtags(card,title);
    const rawId=id?.value || null;
    const stableKey=url || (rawId ? `${id.field}:${rawId}` : null) || (fp ? `thumb:${fp}` : null);
    if(!stableKey) return null;
    return {
      item:{key:stableKey,url,views:metric.value,viewsText:metric.text,title,thumbnail:thumb,thumbnailFingerprint:fp,hashtags,id:rawId,idField:id?.field||null,duration_seconds:extractDuration(card,thumbnail)},
      metric,thumbnail,card,clickTarget:findClickTarget(card,thumbnail)
    };
  }

  function collectDetailed() {
    const map=new Map();
    for(const metric of metricElements()){
      const d=snapshot(metric); if(!d) continue;
      const old=map.get(d.item.key); if(!old || d.item.views>old.item.views) map.set(d.item.key,d);
    }
    return [...map.values()];
  }

  function scrollInfo(el) {
    const isDoc=el===document.scrollingElement||el===document.documentElement||el===document.body;
    const clientHeight=isDoc?window.innerHeight:(el?.clientHeight||0);
    const scrollHeight=el?.scrollHeight||0;
    const top=isDoc?(document.scrollingElement?.scrollTop||window.scrollY||0):(el?.scrollTop||0);
    return {el,isDoc,clientHeight,scrollHeight,top,max:Math.max(0,scrollHeight-clientHeight)};
  }

  function scrollableAncestors(details) {
    const counts=new Map();
    const add=(el,w=1)=>{if(!el)return;const i=scrollInfo(el);if(i.max<5||i.clientHeight<100)return;counts.set(el,(counts.get(el)||0)+w);};
    add(document.scrollingElement||document.documentElement,Math.max(2,details.length));
    for(const d of details){
      let node=d.thumbnail?.element?.parentElement||d.clickTarget?.parentElement;
      for(let depth=0;node&&node!==document.body&&depth<12;depth++,node=node.parentElement){
        const i=scrollInfo(node); if(i.max<5||i.clientHeight<100) continue;
        let oy=''; try{oy=getComputedStyle(node).overflowY;}catch{}
        if(/auto|scroll|overlay|hidden/i.test(oy)) add(node,3); else if(i.max>500) add(node,1);
      }
    }
    const arr=[...counts.entries()].map(([el,count])=>{const i=scrollInfo(el);const r=i.isDoc?{width:innerWidth,height:innerHeight}:(rectOf(el)||{width:0,height:0});const area=Math.max(0,r.width*r.height);return {...i,count,score:count*1_000_000+Math.min(i.max,2_000_000)+Math.min(area,2_000_000)/10};});
    arr.sort((a,b)=>b.score-a.score); return arr;
  }

  function chooseScrollTarget() {
    const details=collectDetailed();
    const candidates=scrollableAncestors(details);
    scrollTarget=candidates[0]?.el || document.scrollingElement || document.documentElement;
    const info=scrollInfo(scrollTarget);
    return {top:info.top,max:info.max,clientHeight:info.clientHeight,scrollHeight:info.scrollHeight,isDoc:info.isDoc,items:details.length};
  }

  function getScrollInfo() {
    if(!scrollTarget || !scrollTarget.isConnected) chooseScrollTarget();
    const i=scrollInfo(scrollTarget);
    return {top:i.top,max:i.max,clientHeight:i.clientHeight,scrollHeight:i.scrollHeight,isDoc:i.isDoc};
  }

  function setScroll(top) {
    if(!scrollTarget || !scrollTarget.isConnected) chooseScrollTarget();
    const i=scrollInfo(scrollTarget); const v=Math.max(0,Math.min(i.max,Number(top)||0));
    if(i.isDoc){try{window.scrollTo(0,v);}catch{} try{if(document.scrollingElement)document.scrollingElement.scrollTop=v;}catch{}}
    else {try{scrollTarget.scrollTop=v;}catch{} try{scrollTarget.scrollTo?.({top:v,behavior:'auto'});}catch{}}
    return getScrollInfo();
  }

  function collectPublic() { return collectDetailed().map(d=>d.item); }

  function targetMatches(d,t) {
    if(t.thumbnailFingerprint && d.item.thumbnailFingerprint && t.thumbnailFingerprint!==d.item.thumbnailFingerprint) return false;
    if(t.photo_id && d.item.id && String(t.photo_id)!==String(d.item.id)) return false;
    if(t.key && d.item.key===t.key) return true;
    if(t.photo_id && d.item.id) return String(t.photo_id)===String(d.item.id);
    if(t.thumbnailFingerprint && d.item.thumbnailFingerprint===t.thumbnailFingerprint) return true;
    return false;
  }

  let linkCandidates = new Map();
  function findTargets(targets) {
    const details=collectDetailed();
    linkCandidates = new Map();
    const byKey=new Map(), byId=new Map(), byThumb=new Map();
    const add=(map,key,d)=>{if(key){if(!map.has(key))map.set(key,[]);map.get(key).push(d);}};
    for(const d of details){
      add(byKey,d.item.key,d);add(byId,d.item.id,d);add(byThumb,d.item.thumbnailFingerprint,d);
    }
    return targets.map(t=>{
      const exact=byKey.get(t.key)||[];
      const candidates=[...new Set([...(byId.get(t.photo_id)||[]),...(byThumb.get(t.thumbnailFingerprint)||[])])];
      const matches=(exact.length?exact:candidates).filter(d=>targetMatches(d,t));
      if(matches.length!==1) return {key:t.key,ok:false,reason:matches.length?'ambiguous':'not-visible'};
      const d=matches[0]; linkCandidates.set(t.key,d);
      return {key:t.key,ok:true,url:d.item.url||null};
    });
  }

  async function clickForTarget(target) {
    let d=linkCandidates.get(target.key);
    if(!d?.clickTarget?.isConnected) {
      findTargets([target]); d=linkCandidates.get(target.key);
    }
    if(!d) return {ok:false,reason:'not-visible'};
    // Virtualized grids can reuse the same node for another video.
    const current=snapshot({...d.metric,text:normalizeText(d.metric.el.innerText||d.metric.el.textContent),rect:rectOf(d.metric.el)});
    if(!current || !targetMatches(current,target)) return {ok:false,reason:'changed-card'};
    d=current;
    try{d.clickTarget?.scrollIntoView?.({block:'center',inline:'nearest',behavior:'auto'});}catch{}
    await sleep(35);
    const checked=snapshot({...d.metric,text:normalizeText(d.metric.el.innerText||d.metric.el.textContent),rect:rectOf(d.metric.el)});
    if(!checked || !targetMatches(checked,target)) return {ok:false,reason:'changed-card'};
    d=checked;
    if(target.realClick){
      const r=rectOf(d.thumbnail.media || d.clickTarget);
      if(!r || r.width<1 || r.height<1) return {ok:false,reason:'not-visible'};
      return {ok:true,key:d.item.key,x:r.left+r.width/2,y:r.top+r.height/2};
    }
    try{d.clickTarget?.click?.();}catch{try{d.clickTarget?.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window}));}catch{}}
    return {ok:true,key:d.item.key};
  }

  function endState(){
    const info=getScrollInfo();
    const root=scrollTarget || document.body;
    const visible=el=>isVisible(el) && (()=>{const r=rectOf(el);return r.bottom>0 && r.top<innerHeight;})();
    const loading=[...root.querySelectorAll('[aria-busy="true"],[role="progressbar"],[class*="loading"],[class*="spinner"]')].some(visible);
    const texts=[...root.querySelectorAll('p,span,small,div')].filter(el=>el.children.length===0 && visible(el)).map(el=>normalizeText(el.textContent));
    const marker=texts.find(t=>/^(?:não há mais (?:vídeos|resultados|conteúdo)|sem mais (?:vídeos|resultados)|você (?:viu|chegou a) tudo|no more (?:videos|results|content)|没有更多了)[.!…\s]*$/iu.test(t)) || null;
    return {...info,loading,marker,atBottom:info.max<=5 || info.top>=info.max-5};
  }

  window.__KPA = {
    version:'2.1.0',
    takeFeed:()=>feedQueue.shift()||null,
    feedCount:()=>feedQueue.length,
    armLink:(token)=>{armedLink={token:String(token||Date.now()),suppress:true};capturedLink=null;return true;},
    disarmLink:()=>{armedLink=null;return true;},
    takeLink:()=>{const x=capturedLink;capturedLink=null;return x;},
    normalizeVideoUrl,
  };

  window.__KPA_DOM = {
    collect:collectPublic,
    findTargets,
    gridKeys:()=>collectPublic().map(item=>item.thumbnailFingerprint||item.key),
    endState,
    chooseScrollTarget,
    getScrollInfo,
    scrollTop:()=>setScroll(0),
    scrollBottom:()=>{if(!scrollTarget||!scrollTarget.isConnected)chooseScrollTarget();const i=scrollInfo(scrollTarget);return setScroll(Math.min(i.max,i.top+Math.max(200,i.clientHeight*0.8)));},
    nudge:()=>{const i=scrollInfo(scrollTarget);return setScroll(Math.max(0,i.top-100));},
    scrollTo:(top)=>setScroll(top),
    clickForTarget,
  };
})();
