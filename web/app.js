/* Duel Masters web client — green-mat playmat layout.
   Drag & drop to play; click any card to read it full-size (with any
   available actions for it); 📜 toggles the game log. */
const PARAMS = new URLSearchParams(location.search);
let GID = PARAMS.get("gid"), SEAT = Number(PARAMS.get("seat") || 0),
    STATE = null, POLL = null, DRAG = null;
const CARDS = {};  // iid -> card json from latest render
let LAST_DRAG_END = 0;

const CIVS = ["Light", "Water", "Darkness", "Fire", "Nature"];

const $ = id => document.getElementById(id);
const api = (path, opts) => fetch("/api" + path, opts).then(async r => {
  if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
  return r.json();
});

/* ------------------------------------------------------------ auto-start
   No setup screen: decks are random, the AI provider/model/key come from
   server env vars (MODEL_ACCESS_KEY / MODEL_NAME / MODEL_BASE_URL, or
   OPENAI_API_KEY / ANTHROPIC_API_KEY). /?hotseat=1 forces a two-human game. */
async function autoStart() {
  try {
    const body = PARAMS.get("hotseat") ? {provider: "none"} : {};
    const res = await api("/games", {method: "POST",
      headers: {"content-type": "application/json"}, body: JSON.stringify(body)});
    GID = res.game_id;
    history.replaceState(null, "", `?gid=${GID}&seat=0`);
    if (res.provider === "none")
      console.info(`Hotseat: open ${location.origin}/?gid=${GID}&seat=1 for player 2`);
    enterBoard();
  } catch (e) {
    $("boot-status").textContent = "Could not start a game.";
    $("boot-error").textContent = e.message;
  }
}

function enterBoard() {
  $("boot").style.display = "none";
  $("board").style.display = "grid";
  initDropZones();
  $("log-toggle").onclick = () => {
    const p = $("log-panel");
    p.style.display = p.style.display === "none" ? "flex" : "none";
  };
  $("card-modal").onclick = e => { if (e.target.id === "card-modal") closeModal(); };
  $("spread").onclick = e => { if (e.target.id !== "spread") return; closeSpread(); };
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") { closeModal(); closeSpread(); }   // no-op if locked
  });
  poll(); POLL = setInterval(poll, 1200);
}

/* ---------------------------------------------------------------- render */
async function poll() {
  if (DRAG) return;              // don't re-render mid-drag
  try { STATE = await api(`/games/${GID}/state?player=${SEAT}`); } catch { return; }
  render();
}

/* Option lookups for the current pending decision (ours only). */
function myOptions() {
  const p = STATE && STATE.pending;
  return (p && p.player === SEAT && p.options) ? p.options : [];
}
const chargeOpt = iid => myOptions().find(o => o.kind === "charge" && o.iid === iid);
const playOpt = iid => myOptions().find(o => o.kind === "play" && o.iid === iid);
const attackOpts = iid => myOptions().filter(o => o.kind === "attack" && o.iid === iid);

/* ------------------------------------------------------------ card modal */
const reEsc = s => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/* The card is shown full-size in the modal, so drop its own name from the
   action label: "Charge Phantom Fish (Water) as mana" -> "Charge as mana". */
function shortLabel(o, c) {
  let t = o.text
    .replace(new RegExp(reEsc(c.name) + "\\s*\\([^)]*\\)", "g"), "")
    .replace(new RegExp(reEsc(c.name), "g"), "")
    .replace(/^\(\d[^)]*\):?/, "")        // leftover "(2000):" power prefix
    .replace(/\s+/g, " ")
    .replace(/:\s*\(/, " (")
    .replace(/\(\s*\)/g, "")
    .replace(/\s+([,.:;)])/g, "$1")
    .replace(/^[\s:,–—-]+|[\s:,–—-]+$/g, "")
    .replace(/\s+(with|as|to|from|for|into)$/i, "");  // "Block with" -> "Block"
  if (!t) t = c.type === "Creature" ? "Summon" : "Cast";
  return t[0].toUpperCase() + t.slice(1);
}

function openModal(iid) {
  if (Date.now() - LAST_DRAG_END < 300) return;  // click ghosted by a drag
  const c = CARDS[iid];
  if (!c) return;
  $("card-modal-img").src = c.image;
  const acts = $("card-modal-actions");
  acts.replaceChildren();
  for (const o of myOptions().filter(o => o.iid === iid || o.target === iid)) {
    const b = document.createElement("button");
    b.textContent = shortLabel(o, c);
    b.onclick = () => { closeModal(); act(o.id); };
    acts.appendChild(b);
  }
  if (!acts.children.length) {
    const hint = document.createElement("div");
    hint.className = "hint";
    hint.textContent = "(no actions available for this card right now — Esc or click away to close)";
    acts.appendChild(hint);
  }
  $("card-modal").style.display = "flex";
}
function closeModal() { $("card-modal").style.display = "none"; }

/* A readable spread of cards: a zone you're browsing (hand, graveyard) or a
   set you must choose from (deck search, graveyard pick). Cards are shown big
   enough to read and clicking one either opens it or answers the decision. */
let SPREAD_LOCKED = false, SPREAD_SIG = null;

function sizeSpread(n) {
  const gap = 14;
  if (n > 8) {                       // a deck search: comfortable grid, scrolls
    $("spread-cards").style.setProperty("--spread-w", "158px");
    return;
  }
  const byWidth = (window.innerWidth * 0.94 - gap * n) / n;
  const byHeight = window.innerHeight * 0.74 * 63 / 88;
  $("spread-cards").style.setProperty("--spread-w",
    `${Math.round(Math.max(170, Math.min(360, byWidth, byHeight)))}px`);
}

function spreadCard(c, {caption = null, onPick = null} = {}) {
  CARDS[c.iid] = c;
  const d = document.createElement("div");
  d.className = "spread-card";
  const img = document.createElement("img");
  img.src = c.image; img.alt = c.name; img.draggable = false;
  d.appendChild(img);
  if (caption) {
    const cap = document.createElement("div");
    cap.className = "spread-cap";
    cap.textContent = caption;
    d.appendChild(cap);
  }
  d.onclick = onPick || (() => { closeSpread(true); openModal(c.iid); });
  return d;
}

function openSpread(title, cards) {
  if (!cards.length) return;
  SPREAD_LOCKED = false; SPREAD_SIG = null;
  $("spread-title").textContent =
    `${title} — ${cards.length} card${cards.length > 1 ? "s" : ""}`;
  sizeSpread(cards.length);
  $("spread-cards").replaceChildren(...cards.map(c => {
    const el = spreadCard(c);
    if (myOptions().some(o => o.iid === c.iid)) el.classList.add("actionable");
    return el;
  }));
  $("spread-actions").replaceChildren();
  $("spread").style.display = "flex";
}

/* Choose one of a set of cards you can't click on the table. */
function openCardChooser(prompt, cardOpts, pillOpts, cardsById) {
  const sig = prompt + "|" + cardOpts.concat(pillOpts).map(o => o.id).join(",");
  if (SPREAD_SIG === sig) return;         // already open for this decision
  SPREAD_SIG = sig; SPREAD_LOCKED = true;
  $("spread-title").textContent = prompt;
  sizeSpread(cardOpts.length);
  $("spread-cards").replaceChildren(...cardOpts.map(o => {
    const c = cardsById[o.iid];
    return spreadCard(c, {
      caption: `${c.name}${c.cost != null ? ` · ${c.cost}` : ""}`,
      onPick: () => { closeSpread(true); act(o.id); },
    });
  }));
  $("spread-actions").replaceChildren(...pillOpts.map(o => {
    const b = document.createElement("button");
    b.textContent = o.text;
    b.onclick = () => { closeSpread(true); act(o.id); };
    return b;
  }));
  $("spread").style.display = "flex";
}

function closeSpread(force = false) {
  if (SPREAD_LOCKED && !force) return;   // a required choice can't be dismissed
  SPREAD_LOCKED = false; SPREAD_SIG = null;
  $("spread").style.display = "none";
}

function cardEl(c, {battle = false, hand = false} = {}) {
  CARDS[c.iid] = c;
  const div = document.createElement("div");
  div.className = "card";
  if (battle && c.tapped) div.classList.add("tapped");
  if (battle && c.sick) div.classList.add("sick");
  const img = document.createElement("img");
  img.src = c.image; img.alt = c.name; img.loading = "lazy"; img.draggable = false;
  div.appendChild(img);
  if (battle && c.type === "Creature") {
    const pw = document.createElement("div");
    pw.className = "pw";
    pw.textContent = c.attack_power > c.current_power
      ? `${c.current_power}+${c.attack_power - c.current_power}` : c.current_power;
    div.appendChild(pw);
  }
  div.dataset.iid = c.iid;
  div.onclick = () => cardClicked(c.iid);

  const draggable = hand ? (chargeOpt(c.iid) || playOpt(c.iid))
                  : battle ? attackOpts(c.iid).length : false;
  if (draggable) {
    div.draggable = true;
    div.ondragstart = e => {
      DRAG = {iid: c.iid, from: hand ? "hand" : "battle"};
      div.classList.add("dragging");
      document.body.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      markDropTargets();
    };
    div.ondragend = () => {
      DRAG = null;
      LAST_DRAG_END = Date.now();
      div.classList.remove("dragging");
      document.body.classList.remove("dragging");
      poll();
    };
  }
  return div;
}

function markDropTargets() {
  if (!DRAG) return;
  if (DRAG.from === "hand") {
    if (chargeOpt(DRAG.iid)) $("my-mana").classList.add("droppable");
    if (playOpt(DRAG.iid)) $("my-battle").classList.add("droppable");
  } else {
    for (const o of attackOpts(DRAG.iid)) {
      if (o.target === "player") {
        $("opp-shield-band").classList.add("droppable");
      } else {
        const el = document.querySelector(`#opp-battle .card[data-iid="${o.target}"]`);
        if (el) el.classList.add("droppable");
      }
    }
  }
}

function dropZone(el, getOption) {
  el.ondragover = e => {
    if (DRAG && getOption()) { e.preventDefault(); el.classList.add("drag-over"); }
  };
  el.ondragleave = () => el.classList.remove("drag-over");
  el.ondrop = e => {
    e.preventDefault();
    el.classList.remove("drag-over");
    const o = getOption();
    if (o) { DRAG = null; act(o.id); }
  };
}

function initDropZones() {
  dropZone($("my-mana"), () => DRAG && DRAG.from === "hand" && chargeOpt(DRAG.iid));
  dropZone($("my-battle"), () => DRAG && DRAG.from === "hand" && playOpt(DRAG.iid));
  dropZone($("opp-shield-band"), () => DRAG && DRAG.from === "battle" &&
    attackOpts(DRAG.iid).find(o => o.target === "player"));
  dropZone($("opp-battle"), () => {
    if (!DRAG || DRAG.from !== "battle") return null;
    const atks = attackOpts(DRAG.iid).filter(o => o.target !== "player");
    return atks.length === 1 ? atks[0] : null;  // unambiguous only
  });
}

function chip(ico, val, label) {
  const d = document.createElement("div");
  d.className = "chip"; d.title = label;
  d.innerHTML = `<span class="ico">${ico}</span>${val}`;
  return d;
}

function orbRow(mana) {
  const row = document.createElement("div");
  row.className = "orbs";
  for (const civ of CIVS) {
    const cards = mana.filter(c => c.civ === civ);
    const open = cards.filter(c => !c.tapped).length;
    const orb = document.createElement("div");
    orb.className = `orb orb-${civ}` + (open ? "" : " dim");
    orb.title = `${civ}: ${open} untapped / ${cards.length} total`;
    const img = document.createElement("img");
    img.src = `/static/civs/${civ}.png`; img.alt = civ; img.draggable = false;
    orb.appendChild(img);
    const n = document.createElement("span");
    n.className = "n"; n.textContent = open;
    orb.appendChild(n);
    row.appendChild(orb);
  }
  return row;
}

function statsBar(el, p, {showHand = false, ownHand = null} = {}) {
  el.replaceChildren();
  if (showHand) {
    const d = document.createElement("div");
    d.className = "chip" + (ownHand ? " clickable" : "");
    d.title = ownHand ? "Click to read your hand" : `${p.hand_count} cards in hand`;
    d.innerHTML = `<img class="handback" src="/static/card-back.webp" draggable="false">${p.hand_count}`;
    if (ownHand) d.onclick = () => openSpread("Your hand", ownHand);
    el.appendChild(d);
  }
  el.appendChild(orbRow(p.mana));
}

function manaZone(el, p) {
  el.replaceChildren(...p.mana.map(c => {
    CARDS[c.iid] = c;
    const d = document.createElement("div");
    d.className = "mana-card" + (c.tapped ? " tapped" : "");
    d.title = `${c.name} (${c.civ})${c.tapped ? " — tapped" : ""}`;
    d.dataset.iid = c.iid;
    const img = document.createElement("img");
    img.src = c.image; img.draggable = false;
    d.appendChild(img);
    d.onclick = () => cardClicked(c.iid);
    return d;
  }));
}

function shieldZone(el, count) {
  el.replaceChildren(...Array.from({length: count}, () => {
    const d = document.createElement("div");
    d.className = "shieldcard";
    return d;
  }));
}

function pile(el, p, kind) {
  const count = kind === "deck" ? p.deck_count : p.grave.length;
  el.replaceChildren();
  const card = document.createElement("div");
  card.className = "pilecard" + (count ? "" : " empty");
  if (kind === "grave") {
    card.classList.add("gravepile");
    const top = p.grave[p.grave.length - 1];
    if (top) {
      CARDS[top.iid] = top;
      const img = document.createElement("img");
      img.src = top.image; img.draggable = false;
      card.appendChild(img);
      card.onclick = () => openSpread(`${p.name}'s graveyard`, [...p.grave].reverse());
      el.title = "Click to read the graveyard";
    }
  }
  el.appendChild(card);
  const n = document.createElement("div");
  n.className = "count"; n.textContent = count;
  el.appendChild(n);
}

function render() {
  const s = STATE, me = s.players[SEAT], opp = s.players[SEAT ^ 1];
  const lastEvent = [...s.log].reverse().find(l => !l.startsWith("— Turn")) || "";
  $("ticker").textContent = s.ai_thinking ? "🤖 thinking…" : lastEvent;

  statsBar($("opp-stats"), opp, {showHand: true});
  statsBar($("my-stats"), me, {showHand: true, ownHand: s.hand});
  manaZone($("opp-mana"), opp);
  manaZone($("my-mana"), me);
  shieldZone($("opp-shield-zone"), opp.shield_count);
  shieldZone($("my-shield-zone"), me.shield_count);
  for (const [el, pl, kind] of [[$("opp-deck"), opp, "deck"], [$("opp-grave"), opp, "grave"],
                                [$("my-deck"), me, "deck"], [$("my-grave"), me, "grave"]])
    pile(el, pl, kind);

  $("opp-battle").replaceChildren(...opp.battle.map(c => cardEl(c, {battle: true})));
  $("my-battle").replaceChildren(...me.battle.map(c => cardEl(c, {battle: true})));
  for (const el of document.querySelectorAll("#opp-battle .card")) {
    dropZone(el, () => DRAG && DRAG.from === "battle" &&
      attackOpts(DRAG.iid).find(o => o.target === Number(el.dataset.iid)));
  }

  $("my-hand").replaceChildren(...s.hand.map(c => cardEl(c, {hand: true})));

  for (const el of document.querySelectorAll(".band, .battle-row, .card"))
    el.classList.remove("droppable", "drag-over");

  renderLog(s.log);
  renderOptions();

  if (s.over) {
    $("gameover-text").textContent = s.winner === SEAT ? "🏆 You win!" : `☠️ ${opp.name} wins`;
    $("gameover").style.display = "flex";
    clearInterval(POLL);
  }
}

function renderLog(lines) {
  const el = $("log");
  el.replaceChildren(...lines.map(l => {
    const d = document.createElement("div");
    d.textContent = l;
    if (l.startsWith("— Turn")) d.className = "turnhead";
    if (l.startsWith("🤖")) d.className = "ai";
    return d;
  }));
  el.scrollTop = el.scrollHeight;
}

function renderOptions() {
  const p = STATE.pending;
  const mine = p && p.player === SEAT && p.options;
  const endBtn = $("end-turn");

  let label = "END TURN", enabled = false;
  if (STATE.over) {
    label = "GAME OVER";
  } else if (!mine) {
    label = STATE.ai_thinking ? "THINKING…" : "ENEMY TURN";
  } else if (p.kind === "main") {
    const et = p.options.find(o => o.kind === "end_turn");
    if (et) { enabled = true; endBtn.onclick = () => act(et.id); }
    else label = "MUST ATTACK";
  } else {
    label = "YOUR CHOICE";
  }
  endBtn.textContent = label;
  endBtn.disabled = !enabled;

  // interrupts (block? / shield trigger / targets / yes-no) pop a dialog;
  // normal main-phase play happens directly on the board.
  // Options whose card is visible on the board are picked by clicking the
  // glowing card itself — only choices with no card to click become buttons.
  if (mine && p.kind !== "main") {
    const onBoard = new Set([...document.querySelectorAll(".card[data-iid], .mana-card[data-iid]")]
      .map(el => Number(el.dataset.iid)));
    const art = p.option_cards || {};
    // cards you can't click on the table (deck search, graveyard pick) get a
    // full-size chooser instead of a list of names
    const offTable = p.options.filter(o => o.iid != null && art[o.iid] && !onBoard.has(o.iid));
    const rest = p.options.filter(o => !offTable.includes(o));
    if (offTable.length) {
      openCardChooser(p.prompt, offTable, rest, art);
      $("interrupt").style.display = "none";
      return;
    }
    closeSpread(true);
    const pills = p.options.filter(o => !(o.iid != null && onBoard.has(o.iid)));
    $("prompt").textContent = p.prompt +
      (pills.length < p.options.length ? " (click a highlighted card)" : "");
    $("options").replaceChildren(...pills.map(o => {
      const b = document.createElement("button");
      b.textContent = o.text;
      b.onclick = () => act(o.id);
      return b;
    }));
    $("interrupt").style.display = "block";
  } else {
    closeSpread(true);
    $("interrupt").style.display = "none";
  }

  if (mine) {
    const iids = new Set(p.options.flatMap(o => [o.iid, o.target]).filter(x => x != null));
    for (const el of document.querySelectorAll(".card, .mana-card"))
      if (iids.has(Number(el.dataset.iid))) el.classList.add("actionable");
  }
}

function cardClicked(iid) {
  const p = STATE.pending;
  // during an interrupt, clicking a card that maps to exactly one option picks it
  if (p && p.player === SEAT && p.options && p.kind !== "main") {
    const matches = p.options.filter(o => o.iid === iid || o.target === iid);
    if (matches.length === 1) { act(matches[0].id); return; }
  }
  openModal(iid);
}

async function act(optionId) {
  DRAG = null;
  try {
    await api(`/games/${GID}/act`, {method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({option_id: optionId, player: SEAT})});
    poll();
  } catch (e) { console.warn(e.message); poll(); }
}

$("new-game") && ($("new-game").onclick = () => location.assign("/"));
if (GID) enterBoard(); else autoStart();
