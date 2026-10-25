/*
 * 语音推理游戏网页客户端。
 *
 * 职责：连接 /ws、绘制平面图与角色、键鼠操作、按住空格说话、播放角色语音、
 * 字幕、录音笔、相册与结局展示。协议见接口约定第 3 节。
 */
"use strict";

/** true：显示所有场内角色；false：只显示与玩家同房间的角色。 */
const SHOW_ALL_CHARACTERS = true;
const STEP_INTERVAL_MS = 120;
const INTERACT_RANGE = 1; // 切比雪夫距离
const PHOTO_WARN_THRESHOLD = 5;
const SUBTITLE_MAX = 4;
const SUBTITLE_TTL_MS = 12000;
const MUFFLED_MERGE_MS = 6000;

const PLAYER_COLOR = "#ffd166";
const CHARACTER_COLORS = ["#5aa9f0", "#f07c7c", "#7fd38c", "#c79cf2", "#f0a35a", "#55d0c4", "#f29bd0", "#9fb2ff"];
const ROOM_HUES = [205, 150, 35, 280, 0, 95, 180, 320, 55, 240];
const FONT = '"PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", "Source Han Sans SC", sans-serif';

const KIND_LABELS = {
  speech: "人声",
  muffled: "模糊",
  enter: "进入",
  leave: "离开",
  footsteps: "脚步",
  sound: "声响",
  reveal: "发现",
  notify: "通知",
  job_done: "完成",
};

const SCENE_LABELS = {
  clock: "时间",
  time: "时间",
  room: "地点",
  location: "地点",
  position: "位置",
  people: "在场",
  characters: "在场",
  visible: "可见",
  activities: "正在发生",
  events: "事件",
  interactables: "物品",
  objects: "物品",
  items: "物品",
  describe: "状态",
  notes: "备注",
  sounds: "声音",
};

const KEY_DIRS = {
  ArrowUp: "up", KeyW: "up",
  ArrowDown: "down", KeyS: "down",
  ArrowLeft: "left", KeyA: "left",
  ArrowRight: "right", KeyD: "right",
};

const $ = (id) => document.getElementById(id);

const el = {
  title: $("scenario-title"),
  conn: $("conn-status"),
  clock: $("clock"),
  remaining: $("remaining"),
  photoBtn: $("photo-btn"),
  submitBtn: $("submit-btn"),
  stage: $("stage"),
  canvas: $("map"),
  nearby: $("nearby"),
  subtitles: $("subtitles"),
  micIndicator: $("mic-indicator"),
  micText: $("mic-text"),
  sayForm: $("say-form"),
  sayInput: $("say-input"),
  sayBtn: $("say-btn"),
  recorder: $("recorder"),
  recorderTime: $("recorder-time"),
  album: $("album"),
  albumCount: $("album-count"),
  albumWarning: $("album-warning"),
  toasts: $("toasts"),
  introOverlay: $("intro-overlay"),
  introTitle: $("intro-title"),
  introText: $("intro-text"),
  startBtn: $("start-btn"),
  confirmOverlay: $("confirm-overlay"),
  confirmOk: $("confirm-ok"),
  confirmCancel: $("confirm-cancel"),
  endedOverlay: $("ended-overlay"),
  endedReason: $("ended-reason"),
  endedTitle: $("ended-title"),
  endedSummary: $("ended-summary"),
  endedPoints: $("ended-points"),
  reloadBtn: $("reload-btn"),
};

const game = {
  ws: null,
  playerId: null,
  scenario: null,
  map: null, // buildMap 的结果
  state: null,
  started: false,
  ended: false,
  submitting: false,
  audioCtx: null,
  audioOut: null,
  mic: null,
  micError: null,
  talking: false,
  stopping: false,
  spaceHeld: false,
  heldDirs: [],
  stepTimer: null,
  chars: new Map(), // id -> {data, x, y, tx, ty, color}
  colorIndex: 0,
  colors: new Map(),
  nearby: [],
  nearbyKey: "",
  recorder: [],
  photos: [],
  subtitles: [],
  clickMarker: null, // {x, y, ok, at}
  hoverCell: null,
  flashAt: 0,
  micLevel: 0,
};

/* ------------------------------------------------------------------ 连接 */

function wsUrl() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/ws`;
}

function connect() {
  setConn("connecting");
  const ws = new WebSocket(wsUrl());
  ws.binaryType = "arraybuffer";
  ws.onopen = () => setConn("open");
  ws.onclose = () => {
    setConn("closed");
    stopTalking();
    if (!game.ended) toast("与服务器的连接已断开，请刷新页面重新连接", "error", 8000);
  };
  ws.onerror = () => setConn("closed");
  ws.onmessage = (event) => {
    if (typeof event.data === "string") {
      let msg;
      try {
        msg = JSON.parse(event.data);
      } catch (err) {
        console.warn("无法解析的消息", event.data);
        return;
      }
      handleMessage(msg);
    } else {
      handleAudio(event.data);
    }
  };
  game.ws = ws;
}

function send(msg) {
  if (game.ws && game.ws.readyState === WebSocket.OPEN) game.ws.send(JSON.stringify(msg));
}

function sendBinary(int16) {
  if (game.ws && game.ws.readyState === WebSocket.OPEN) game.ws.send(int16.buffer);
}

function setConn(status) {
  el.conn.className = `conn conn-${status}`;
  el.conn.title = { connecting: "连接中", open: "已连接", closed: "已断开" }[status] || "";
}

function handleMessage(msg) {
  switch (msg.type) {
    case "init": return onInit(msg);
    case "state": return onState(msg);
    case "percept": return onPercept(msg);
    case "speech": return onSpeech(msg);
    case "photo": return onPhoto(msg);
    case "ended": return onEnded(msg);
    default:
      // 约定之外的消息：若带有文本则以提示形式展示，便于服务端反馈操作结果
      if (msg && typeof msg.text === "string" && msg.text) toast(msg.text);
  }
}

function handleAudio(buffer) {
  if (!game.audioOut || game.ended) return; // 点击「开始」之前没有 AudioContext，丢弃
  game.audioOut.handleFrame(buffer);
}

/* ------------------------------------------------------------------ 初始化与地图 */

function onInit(msg) {
  game.playerId = msg.player_id;
  game.scenario = msg.scenario || {};
  game.map = buildMap(game.scenario);
  const title = game.scenario.title || "未命名剧本";
  document.title = title;
  el.title.textContent = title;
  el.introTitle.textContent = title;
  el.introText.textContent = game.scenario.intro || "";
  if (game.scenario.duration_s != null) el.remaining.textContent = formatDuration(game.scenario.duration_s);
  el.startBtn.disabled = false;
  resizeCanvas();
}

function buildMap(scenario) {
  const map = scenario.map || {};
  const rows = map.rows || [];
  const legend = map.legend || {};
  const rooms = scenario.rooms || {};
  const height = map.height || rows.length;
  const width = map.width || rows.reduce((m, r) => Math.max(m, r.length), 0);

  const cells = [];
  const roomCells = new Map();
  for (let y = 0; y < height; y++) {
    const row = [];
    const line = rows[y] || "";
    for (let x = 0; x < width; x++) {
      const ch = x < line.length ? line[x] : " ";
      const def = legend[ch] || { kind: "void" };
      const cell = { kind: def.kind || "void", room: def.room || null, allowed: def.allowed || null };
      row.push(cell);
      if (cell.kind === "floor" && cell.room) {
        if (!roomCells.has(cell.room)) roomCells.set(cell.room, []);
        roomCells.get(cell.room).push([x, y]);
      }
    }
    cells.push(row);
  }

  const roomInfo = new Map();
  const roomIds = [...new Set([...Object.keys(rooms), ...roomCells.keys()])];
  roomIds.forEach((id, i) => {
    const list = roomCells.get(id) || [];
    roomInfo.set(id, {
      id,
      name: (rooms[id] && rooms[id].name) || id,
      offstage: !!(rooms[id] && rooms[id].offstage),
      hue: ROOM_HUES[i % ROOM_HUES.length],
      cells: list,
      label: roomLabelPos(list),
    });
  });

  const interactables = (scenario.interactables || []).filter((it) => Array.isArray(it.pos));
  const blocked = new Set(interactables.map((it) => `${it.pos[0]},${it.pos[1]}`));
  return { width, height, cells, rooms: roomInfo, interactables, blocked };
}

/** 房间名称的标注位置：取房间格子的质心，若质心不在房间内则取最近的房间格。 */
function roomLabelPos(list) {
  if (list.length === 0) return null;
  let sx = 0;
  let sy = 0;
  for (const [x, y] of list) {
    sx += x;
    sy += y;
  }
  const cx = sx / list.length;
  const cy = sy / list.length;
  let best = list[0];
  let bestD = Infinity;
  for (const p of list) {
    const d = (p[0] - cx) ** 2 + (p[1] - cy) ** 2;
    if (d < bestD) {
      bestD = d;
      best = p;
    }
  }
  return bestD < 0.3 ? [cx, cy] : best;
}

function cellAt(x, y) {
  const m = game.map;
  if (!m || x < 0 || y < 0 || x >= m.width || y >= m.height) return null;
  return m.cells[y][x];
}

/* ------------------------------------------------------------------ 状态 */

function onState(msg) {
  game.state = msg;
  if (msg.clock) el.clock.textContent = msg.clock;
  if (msg.remaining_s != null) {
    el.remaining.textContent = formatDuration(msg.remaining_s);
    el.remaining.classList.toggle("warn", msg.remaining_s <= 180 && msg.remaining_s > 60);
    el.remaining.classList.toggle("danger", msg.remaining_s <= 60);
  }
  if (msg.t != null) el.recorderTime.textContent = `REC ${formatDuration(msg.t)}`;

  const player = (msg.characters || []).find((c) => c.id === game.playerId);
  const seen = new Set();
  for (const c of msg.characters || []) {
    if (c.offstage || !Array.isArray(c.pos)) continue;
    if (!SHOW_ALL_CHARACTERS && c.id !== game.playerId) {
      if (!player || !player.room || c.room !== player.room) continue;
    }
    seen.add(c.id);
    let entry = game.chars.get(c.id);
    if (!entry) {
      entry = { x: c.pos[0], y: c.pos[1], color: colorFor(c) };
      game.chars.set(c.id, entry);
    }
    entry.data = c;
    entry.tx = c.pos[0];
    entry.ty = c.pos[1];
    if (Math.abs(entry.tx - entry.x) + Math.abs(entry.ty - entry.y) > 4) {
      entry.x = entry.tx;
      entry.y = entry.ty;
    }
  }
  for (const id of [...game.chars.keys()]) if (!seen.has(id)) game.chars.delete(id);

  updateNearby(player, msg.interactables || []);
}

function colorFor(c) {
  if (c.id === game.playerId) return PLAYER_COLOR;
  if (!game.colors.has(c.id)) {
    game.colors.set(c.id, CHARACTER_COLORS[game.colorIndex % CHARACTER_COLORS.length]);
    game.colorIndex += 1;
  }
  return game.colors.get(c.id);
}

function playerData() {
  const p = game.chars.get(game.playerId);
  return p ? p.data : null;
}

/* ------------------------------------------------------------------ 附近可交互点 */

function updateNearby(player, stateInteractables) {
  const describe = new Map(stateInteractables.map((it) => [it.id, it.describe || []]));
  let list = [];
  if (player && game.map) {
    const [px, py] = player.pos;
    list = game.map.interactables
      .map((it) => ({ it, d: Math.max(Math.abs(it.pos[0] - px), Math.abs(it.pos[1] - py)) }))
      .filter((e) => e.d <= INTERACT_RANGE)
      .sort((a, b) => a.d - b.d)
      .map((e) => ({ ...e.it, describe: describe.get(e.it.id) || [] }));
  }
  game.nearby = list;
  const key = JSON.stringify(list.map((it) => [it.id, it.describe]));
  if (key === game.nearbyKey) return;
  game.nearbyKey = key;
  renderNearby();
}

function renderNearby() {
  el.nearby.replaceChildren();
  el.nearby.classList.toggle("hidden", game.nearby.length === 0);
  let first = true;
  for (const it of game.nearby) {
    const card = document.createElement("div");
    card.className = "nearby-item";
    const name = document.createElement("div");
    name.className = "nearby-name";
    name.textContent = it.name || it.id;
    card.appendChild(name);
    for (const line of it.describe) {
      const d = document.createElement("div");
      d.className = "nearby-desc";
      d.textContent = typeof line === "string" ? line : JSON.stringify(line);
      card.appendChild(d);
    }
    const actions = document.createElement("div");
    actions.className = "nearby-actions";
    for (const action of it.actions || []) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn-small";
      btn.textContent = action.label || action.id;
      if (first) {
        const k = document.createElement("kbd");
        k.textContent = "E";
        btn.appendChild(k);
        first = false;
      }
      btn.addEventListener("click", () => doInteract(it.id, action.id, action.label));
      actions.appendChild(btn);
    }
    if (actions.childElementCount) card.appendChild(actions);
    el.nearby.appendChild(card);
  }
}

function doInteract(target, action, label) {
  if (!playable()) return;
  send({ type: "interact", target, action });
  if (label) toast(`开始：${label}`);
}

function interactFirst() {
  for (const it of game.nearby) {
    const action = (it.actions || [])[0];
    if (action) return doInteract(it.id, action.id, action.label);
  }
}

/* ------------------------------------------------------------------ 字幕 */

function onSpeech(msg) {
  const now = performance.now();
  const muffled = msg.level === "muffled";
  if (muffled) {
    const last = game.subtitles[game.subtitles.length - 1];
    if (last && last.muffled && last.cid === msg.cid && now - last.at < MUFFLED_MERGE_MS) {
      last.at = now;
      last.node.classList.remove("fading");
      return;
    }
  }
  const node = document.createElement("div");
  node.className = muffled ? "subtitle muffled" : "subtitle";
  if (muffled) {
    node.textContent = "隐约听到有人说话……";
  } else {
    const who = document.createElement("span");
    who.className = "subtitle-speaker";
    const isSelf = msg.cid === game.playerId;
    who.textContent = isSelf ? "你" : msg.name || msg.cid || "？";
    const entry = game.chars.get(msg.cid);
    who.style.color = isSelf ? PLAYER_COLOR : entry ? entry.color : "#dfe6f2";
    const text = document.createElement("span");
    text.className = "subtitle-text";
    text.textContent = msg.sentence || "";
    node.append(who, text);
    if (typeof msg.gain === "number" && msg.gain < 0.6) node.classList.add("far");
  }
  el.subtitles.appendChild(node);
  game.subtitles.push({ cid: msg.cid, muffled, at: now, node });
  while (game.subtitles.length > SUBTITLE_MAX) game.subtitles.shift().node.remove();
}

function pruneSubtitles(now) {
  for (const s of game.subtitles) {
    if (now - s.at > SUBTITLE_TTL_MS - 1500) s.node.classList.add("fading");
  }
  while (game.subtitles.length && now - game.subtitles[0].at > SUBTITLE_TTL_MS) {
    game.subtitles.shift().node.remove();
  }
}

/* ------------------------------------------------------------------ 录音笔 */

function onPercept(msg) {
  const item = { t: Number(msg.t) || 0, clock: msg.clock || "", kind: msg.kind || "", text: msg.text || "" };
  // 按游戏时间有序插入（同一时刻按到达顺序）
  let i = game.recorder.length;
  while (i > 0 && game.recorder[i - 1].t > item.t) i--;
  game.recorder.splice(i, 0, item);

  const li = document.createElement("li");
  li.className = `rec-item rec-${item.kind}`;
  const time = document.createElement("span");
  time.className = "rec-time";
  time.textContent = item.clock || formatDuration(item.t);
  const kind = document.createElement("span");
  kind.className = "rec-kind";
  kind.textContent = KIND_LABELS[item.kind] || item.kind || "记录";
  const text = document.createElement("span");
  text.className = "rec-text";
  text.textContent = item.text;
  li.append(time, kind, text);

  const box = el.recorder;
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 24;
  const before = box.children[i] || null;
  box.insertBefore(li, before);
  if (atBottom) box.scrollTop = box.scrollHeight;
}

/* ------------------------------------------------------------------ 相册 */

function takePhoto() {
  if (!playable()) return;
  send({ type: "photo" });
  game.flashAt = performance.now();
}

function onPhoto(msg) {
  game.photos.push({ index: msg.index, scene: msg.scene });
  const count = game.photos.length;
  el.albumCount.textContent = `${count} 张`;
  el.albumWarning.classList.toggle("hidden", count <= PHOTO_WARN_THRESHOLD);

  const empty = el.album.querySelector(".empty");
  if (empty) empty.remove();
  const card = document.createElement("article");
  card.className = "photo-card new";
  const head = document.createElement("div");
  head.className = "photo-head";
  const num = document.createElement("span");
  num.className = "photo-index";
  num.textContent = `#${count}`;
  head.appendChild(num);
  const scene = msg.scene;
  const meta = [];
  if (scene && typeof scene === "object" && !Array.isArray(scene)) {
    for (const k of ["clock", "time", "room", "location"]) {
      if (typeof scene[k] === "string" && scene[k]) meta.push(scene[k]);
    }
  }
  const metaEl = document.createElement("span");
  metaEl.className = "photo-meta";
  metaEl.textContent = meta.join(" · ");
  head.appendChild(metaEl);
  card.appendChild(head);
  card.appendChild(renderScene(scene));
  el.album.prepend(card);
  setTimeout(() => card.classList.remove("new"), 1200);
  toast(`已拍照（第 ${count} 张）`);
}

/** 以通用方式展示照片的场景摘要：字符串直接显示，数组为列表，对象逐项展示。 */
function renderScene(scene) {
  const body = document.createElement("div");
  body.className = "photo-body";
  if (scene == null) return body;
  if (typeof scene !== "object") {
    body.appendChild(textBlock(String(scene)));
    return body;
  }
  if (Array.isArray(scene)) {
    body.appendChild(renderValue(scene));
    return body;
  }
  const skip = new Set(["t", "index", "clock", "time", "room", "location"]);
  for (const k of ["text", "summary", "description"]) {
    if (typeof scene[k] === "string" && scene[k]) {
      body.appendChild(textBlock(scene[k]));
      skip.add(k);
    }
  }
  for (const [k, v] of Object.entries(scene)) {
    if (skip.has(k) || v == null || v === "" || (Array.isArray(v) && v.length === 0)) continue;
    const row = document.createElement("div");
    row.className = "scene-row";
    const label = document.createElement("span");
    label.className = "scene-label";
    label.textContent = SCENE_LABELS[k] || k;
    row.append(label, renderValue(v));
    body.appendChild(row);
  }
  return body;
}

function renderValue(v) {
  if (Array.isArray(v)) {
    const ul = document.createElement("ul");
    for (const item of v) {
      const li = document.createElement("li");
      li.appendChild(renderValue(item));
      ul.appendChild(li);
    }
    return ul;
  }
  if (v && typeof v === "object") {
    // 常见形态 {name, text} / {name, activity}：拼成一句
    const parts = Object.values(v).filter((x) => x != null && typeof x !== "object" && x !== "");
    if (parts.length === Object.keys(v).length) return textBlock(parts.join(" "));
    const box = document.createElement("div");
    for (const [k, val] of Object.entries(v)) {
      const row = document.createElement("div");
      row.className = "scene-row";
      const label = document.createElement("span");
      label.className = "scene-label";
      label.textContent = SCENE_LABELS[k] || k;
      row.append(label, renderValue(val));
      box.appendChild(row);
    }
    return box;
  }
  return textBlock(String(v));
}

function textBlock(text) {
  const span = document.createElement("span");
  span.className = "scene-text";
  span.textContent = text;
  return span;
}

/* ------------------------------------------------------------------ 提交与结局 */

function askSubmit() {
  if (!playable() || game.submitting) return;
  el.confirmOverlay.classList.remove("hidden");
  el.confirmOk.focus();
}

function confirmSubmit() {
  el.confirmOverlay.classList.add("hidden");
  if (!playable()) return;
  game.submitting = true;
  send({ type: "submit" });
  el.submitBtn.disabled = true;
  el.submitBtn.textContent = "评判中…";
  stopTalking();
}

function onEnded(msg) {
  game.ended = true;
  stopTalking();
  releaseDirs();
  if (game.audioOut) game.audioOut.stopAll();
  el.confirmOverlay.classList.add("hidden");
  el.introOverlay.classList.add("hidden");
  setInputsEnabled(false);
  el.submitBtn.textContent = "已结束";

  el.endedReason.textContent = { timeout: "时间到", submitted: "已提交证据" }[msg.reason] || "本局结束";
  let verdict = msg.verdict;
  if (typeof verdict === "string") verdict = { summary: verdict };
  verdict = verdict && typeof verdict === "object" ? verdict : {};
  setOptionalText(el.endedTitle, verdict.title);
  setOptionalText(el.endedSummary, verdict.summary);
  el.endedPoints.replaceChildren();
  const points = Array.isArray(verdict.points) ? verdict.points : [];
  for (const p of points) {
    const li = document.createElement("li");
    li.appendChild(renderValue(p));
    el.endedPoints.appendChild(li);
  }
  el.endedPoints.classList.toggle("hidden", points.length === 0);
  el.endedOverlay.classList.remove("hidden");
}

function setOptionalText(node, value) {
  const has = typeof value === "string" ? value.length > 0 : value != null;
  node.textContent = has ? String(value) : "";
  node.classList.toggle("hidden", !has);
}

/* ------------------------------------------------------------------ 开始 */

async function start() {
  if (game.started || !game.scenario) return;
  game.started = true;
  el.introOverlay.classList.add("hidden");
  // 玩家读完背景介绍后，服务端才开始计时
  send({ type: "start" });
  const Ctx = window.AudioContext || window.webkitAudioContext;
  try {
    game.audioCtx = new Ctx({ latencyHint: "interactive" });
    await game.audioCtx.resume();
    game.audioOut = new AudioOut(game.audioCtx);
    game.mic = new MicCapture(game.audioCtx, {
      onFrame: (frame) => {
        if (game.talking) sendBinary(frame);
      },
      onLevel: (rms) => {
        game.micLevel = rms;
      },
    });
  } catch (err) {
    console.error(err);
    toast("无法初始化音频，将听不到角色语音", "error");
  }
  setInputsEnabled(true);
  if (document.activeElement) document.activeElement.blur();
  // 提前申请麦克风权限，避免第一次说话时才弹出授权
  if (game.mic) {
    game.mic.open().catch((err) => {
      game.micError = err;
      el.micIndicator.classList.add("unavailable");
      el.micText.textContent = "麦克风不可用，请用文本框说话";
      toast(`无法使用麦克风：${err.message || err.name}`, "error", 6000);
    });
  }
}

function setInputsEnabled(on) {
  el.photoBtn.disabled = !on;
  el.submitBtn.disabled = !on;
  el.sayInput.disabled = !on;
  el.sayBtn.disabled = !on;
}

function playable() {
  return game.started && !game.ended;
}

/* ------------------------------------------------------------------ 说话 */

async function startTalking() {
  game.spaceHeld = true;
  if (!playable() || game.talking || !game.mic || game.micError) return;
  try {
    await game.mic.open();
  } catch (err) {
    return; // 已在 start() 中提示
  }
  if (!game.spaceHeld || game.talking || !playable()) return;
  if (game.audioCtx.state !== "running") await game.audioCtx.resume();
  game.talking = true;
  send({ type: "mic", state: "start" });
  game.mic.start();
  el.micIndicator.classList.add("live");
  el.micText.textContent = "正在说话… 松开空格结束";
}

async function stopTalking() {
  game.spaceHeld = false;
  if (!game.talking || game.stopping) return;
  game.stopping = true;
  await game.mic.stop(); // 最后一帧经 onFrame 发出（此时 talking 仍为 true）
  game.stopping = false;
  game.talking = false;
  send({ type: "mic", state: "stop" });
  el.micIndicator.classList.remove("live");
  el.micText.innerHTML = "按住 <kbd>空格</kbd> 说话";
}

function sayText(event) {
  event.preventDefault();
  const text = el.sayInput.value.trim();
  if (!text || !playable()) return;
  send({ type: "say_text", text });
  el.sayInput.value = "";
  el.sayInput.blur();
}

/* ------------------------------------------------------------------ 键盘与鼠标 */

function onKeyDown(event) {
  if (!el.confirmOverlay.classList.contains("hidden")) {
    if (event.code === "Escape") el.confirmOverlay.classList.add("hidden");
    return;
  }
  if (event.target === el.sayInput) {
    if (event.code === "Escape") el.sayInput.blur();
    return;
  }
  if (!game.started && (event.code === "Enter" || event.code === "Space") && !el.startBtn.disabled) {
    event.preventDefault();
    start();
    return;
  }
  if (!playable() || event.ctrlKey || event.metaKey || event.altKey) return;
  // 鼠标点过的按钮会保留焦点，避免空格/回车再次触发它
  if (document.activeElement && document.activeElement.tagName === "BUTTON") document.activeElement.blur();

  const dir = KEY_DIRS[event.code];
  if (dir) {
    event.preventDefault();
    if (!event.repeat && !game.heldDirs.includes(dir)) {
      game.heldDirs.push(dir);
      send({ type: "step", dir });
      if (!game.stepTimer) game.stepTimer = setInterval(stepTick, STEP_INTERVAL_MS);
    }
    return;
  }
  switch (event.code) {
    case "Space":
      event.preventDefault();
      if (!event.repeat) startTalking();
      break;
    case "KeyE":
      if (!event.repeat) interactFirst();
      break;
    case "KeyP":
      if (!event.repeat) takePhoto();
      break;
    case "Escape":
      send({ type: "stop" });
      break;
    case "Enter":
    case "KeyT":
      event.preventDefault();
      el.sayInput.focus();
      break;
    default:
  }
}

function onKeyUp(event) {
  const dir = KEY_DIRS[event.code];
  if (dir) {
    game.heldDirs = game.heldDirs.filter((d) => d !== dir);
    if (game.heldDirs.length === 0) clearStepTimer();
    return;
  }
  if (event.code === "Space") {
    if (playable()) event.preventDefault();
    stopTalking();
  }
}

function stepTick() {
  if (!playable() || game.heldDirs.length === 0) return clearStepTimer();
  send({ type: "step", dir: game.heldDirs[game.heldDirs.length - 1] });
}

function clearStepTimer() {
  if (game.stepTimer) clearInterval(game.stepTimer);
  game.stepTimer = null;
}

function releaseDirs() {
  game.heldDirs = [];
  clearStepTimer();
}

function eventCell(event) {
  const rect = el.canvas.getBoundingClientRect();
  const x = Math.floor((event.clientX - rect.left - view.ox) / view.cs);
  const y = Math.floor((event.clientY - rect.top - view.oy) / view.cs);
  return cellAt(x, y) ? { x, y } : null;
}

function isTargetable(x, y) {
  const cell = cellAt(x, y);
  if (!cell) return false;
  if (game.map.blocked.has(`${x},${y}`)) return true; // 点击可交互点：走到它旁边
  if (cell.kind !== "floor" && cell.kind !== "door") return false;
  const room = cell.room && game.map.rooms.get(cell.room);
  return !(room && room.offstage);
}

function onCanvasClick(event) {
  el.sayInput.blur();
  if (!playable()) return;
  const c = eventCell(event);
  if (!c) return;
  const ok = isTargetable(c.x, c.y);
  game.clickMarker = { x: c.x, y: c.y, ok, at: performance.now() };
  if (ok) send({ type: "move_to", pos: [c.x, c.y] });
}

function onCanvasMove(event) {
  const c = eventCell(event);
  game.hoverCell = c;
  el.canvas.style.cursor = c && playable() && isTargetable(c.x, c.y) ? "pointer" : "default";
}

/* ------------------------------------------------------------------ 绘制 */

const view = { cs: 16, ox: 0, oy: 0, w: 0, h: 0, dpr: 1, staticLayer: null };

function resizeCanvas() {
  const rect = el.stage.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  view.w = rect.width;
  view.h = rect.height;
  view.dpr = dpr;
  el.canvas.width = Math.round(rect.width * dpr);
  el.canvas.height = Math.round(rect.height * dpr);
  el.canvas.style.width = `${rect.width}px`;
  el.canvas.style.height = `${rect.height}px`;
  if (!game.map || game.map.width === 0) return;
  const pad = 16;
  const cs = Math.max(4, Math.floor(Math.min((rect.width - pad * 2) / game.map.width, (rect.height - pad * 2) / game.map.height)));
  view.cs = cs;
  view.ox = Math.round((rect.width - cs * game.map.width) / 2);
  view.oy = Math.round((rect.height - cs * game.map.height) / 2);
  renderStatic();
}

function renderStatic() {
  const m = game.map;
  const layer = document.createElement("canvas");
  layer.width = el.canvas.width;
  layer.height = el.canvas.height;
  const ctx = layer.getContext("2d");
  ctx.setTransform(view.dpr, 0, 0, view.dpr, 0, 0);
  const cs = view.cs;
  const X = (x) => view.ox + x * cs;
  const Y = (y) => view.oy + y * cs;

  // 地板与门
  for (let y = 0; y < m.height; y++) {
    for (let x = 0; x < m.width; x++) {
      const cell = m.cells[y][x];
      if (cell.kind === "floor") {
        const room = cell.room ? m.rooms.get(cell.room) : null;
        ctx.fillStyle = "#222a37";
        ctx.fillRect(X(x), Y(y), cs, cs);
        if (room) {
          ctx.fillStyle = room.offstage ? `hsla(${room.hue}, 10%, 55%, 0.10)` : `hsla(${room.hue}, 55%, 66%, 0.17)`;
          ctx.fillRect(X(x), Y(y), cs, cs);
        }
        ctx.strokeStyle = "rgba(255,255,255,0.035)";
        ctx.lineWidth = 1;
        ctx.strokeRect(X(x) + 0.5, Y(y) + 0.5, cs - 1, cs - 1);
      } else if (cell.kind === "door") {
        ctx.fillStyle = "#222a37";
        ctx.fillRect(X(x), Y(y), cs, cs);
        const restricted = Array.isArray(cell.allowed) && cell.allowed.length > 0;
        ctx.fillStyle = restricted ? "#b5565c" : "#c08a4e";
        const inset = Math.max(1, cs * 0.12);
        roundRect(ctx, X(x) + inset, Y(y) + inset, cs - inset * 2, cs - inset * 2, cs * 0.12);
        ctx.fill();
        if (restricted) drawLock(ctx, X(x) + cs / 2, Y(y) + cs / 2, cs * 0.22);
      }
    }
  }

  // 场外房间斜线
  for (const room of m.rooms.values()) {
    if (!room.offstage) continue;
    ctx.save();
    ctx.strokeStyle = "rgba(255,255,255,0.05)";
    ctx.lineWidth = 1;
    for (const [x, y] of room.cells) {
      ctx.beginPath();
      ctx.moveTo(X(x), Y(y + 1));
      ctx.lineTo(X(x + 1), Y(y));
      ctx.stroke();
    }
    ctx.restore();
  }

  // 墙
  for (let y = 0; y < m.height; y++) {
    for (let x = 0; x < m.width; x++) {
      if (m.cells[y][x].kind !== "wall") continue;
      ctx.fillStyle = "#545e73";
      ctx.fillRect(X(x), Y(y), cs, cs);
      ctx.fillStyle = "rgba(255,255,255,0.08)";
      ctx.fillRect(X(x), Y(y), cs, Math.max(1, cs * 0.15));
    }
  }

  // 房间名称
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  const roomFont = Math.max(12, Math.min(22, Math.round(cs * 0.85)));
  for (const room of m.rooms.values()) {
    if (!room.label) continue;
    const lx = X(room.label[0] + 0.5);
    const ly = Y(room.label[1] + 0.5);
    ctx.font = `600 ${roomFont}px ${FONT}`;
    ctx.fillStyle = room.offstage ? "rgba(220,228,240,0.22)" : `hsla(${room.hue}, 60%, 82%, 0.38)`;
    ctx.fillText(room.offstage ? `${room.name}（场外）` : room.name, lx, ly);
  }

  // 可交互点
  const itemFont = Math.max(10, Math.min(14, Math.round(cs * 0.55)));
  for (const it of m.interactables) {
    const [x, y] = it.pos;
    drawInteractableIcon(ctx, X(x) + cs / 2, Y(y) + cs / 2, cs);
    ctx.font = `${itemFont}px ${FONT}`;
    drawLabel(ctx, it.name || it.id, X(x) + cs / 2, Y(y) + cs + itemFont * 0.7, "#f3d68d");
  }

  view.staticLayer = layer;
}

function drawInteractableIcon(ctx, cx, cy, cs) {
  const s = cs * 0.8;
  ctx.fillStyle = "#e9b949";
  roundRect(ctx, cx - s / 2, cy - s / 2, s, s, s * 0.25);
  ctx.fill();
  ctx.strokeStyle = "rgba(0,0,0,0.35)";
  ctx.lineWidth = 1;
  ctx.stroke();
  // 四角星
  const r = s * 0.32;
  const k = r * 0.32;
  ctx.fillStyle = "#3a2a08";
  ctx.beginPath();
  ctx.moveTo(cx, cy - r);
  ctx.lineTo(cx + k, cy - k);
  ctx.lineTo(cx + r, cy);
  ctx.lineTo(cx + k, cy + k);
  ctx.lineTo(cx, cy + r);
  ctx.lineTo(cx - k, cy + k);
  ctx.lineTo(cx - r, cy);
  ctx.lineTo(cx - k, cy - k);
  ctx.closePath();
  ctx.fill();
}

function drawLock(ctx, cx, cy, r) {
  ctx.fillStyle = "rgba(20,10,10,0.75)";
  ctx.fillRect(cx - r, cy - r * 0.2, r * 2, r * 1.4);
  ctx.strokeStyle = "rgba(20,10,10,0.75)";
  ctx.lineWidth = Math.max(1, r * 0.35);
  ctx.beginPath();
  ctx.arc(cx, cy - r * 0.2, r * 0.6, Math.PI, 0);
  ctx.stroke();
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function drawLabel(ctx, text, x, y, color) {
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.lineJoin = "round";
  ctx.lineWidth = 3;
  ctx.strokeStyle = "rgba(10,13,19,0.85)";
  ctx.strokeText(text, x, y);
  ctx.fillStyle = color;
  ctx.fillText(text, x, y);
}

let lastFrame = performance.now();

function frame(now) {
  const dt = Math.min(0.1, (now - lastFrame) / 1000);
  lastFrame = now;
  pruneSubtitles(now);
  updateMicIndicator();

  const ctx = el.canvas.getContext("2d");
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, el.canvas.width, el.canvas.height);
  if (game.map && view.staticLayer) {
    ctx.drawImage(view.staticLayer, 0, 0);
    ctx.setTransform(view.dpr, 0, 0, view.dpr, 0, 0);
    drawDynamic(ctx, now, dt);
  }
  requestAnimationFrame(frame);
}

function drawDynamic(ctx, now, dt) {
  const cs = view.cs;
  const X = (x) => view.ox + x * cs;
  const Y = (y) => view.oy + y * cs;
  const player = playerData();

  // 玩家所在房间加亮
  if (player && player.room && game.map.rooms.has(player.room)) {
    const room = game.map.rooms.get(player.room);
    ctx.fillStyle = `hsla(${room.hue}, 60%, 70%, 0.07)`;
    for (const [x, y] of room.cells) ctx.fillRect(X(x), Y(y), cs, cs);
  }

  // 悬停格
  if (game.hoverCell && playable() && isTargetable(game.hoverCell.x, game.hoverCell.y)) {
    ctx.strokeStyle = "rgba(255,255,255,0.35)";
    ctx.lineWidth = 1.5;
    ctx.strokeRect(X(game.hoverCell.x) + 1, Y(game.hoverCell.y) + 1, cs - 2, cs - 2);
  }

  // 附近可交互点的提示光圈
  const pulse = 0.5 + 0.5 * Math.sin(now / 280);
  for (const it of game.nearby) {
    const [x, y] = it.pos;
    ctx.strokeStyle = `rgba(255,214,102,${0.45 + 0.45 * pulse})`;
    ctx.lineWidth = 2;
    roundRect(ctx, X(x) - 3, Y(y) - 3, cs + 6, cs + 6, cs * 0.3);
    ctx.stroke();
  }

  // 点击目标标记
  const mk = game.clickMarker;
  if (mk) {
    const age = (now - mk.at) / 900;
    if (age >= 1) {
      game.clickMarker = null;
    } else {
      ctx.strokeStyle = mk.ok ? `rgba(120,200,255,${1 - age})` : `rgba(255,110,110,${1 - age})`;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(X(mk.x + 0.5), Y(mk.y + 0.5), cs * (0.25 + age * 0.45), 0, Math.PI * 2);
      ctx.stroke();
    }
  }

  // 角色：平滑插值，同格多人错开
  const k = Math.min(1, dt * 10);
  const groups = new Map();
  for (const entry of game.chars.values()) {
    entry.x += (entry.tx - entry.x) * k;
    entry.y += (entry.ty - entry.y) * k;
    const key = `${entry.tx},${entry.ty}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(entry);
  }
  const ordered = [...game.chars.values()].sort((a, b) => (a.data.id === game.playerId) - (b.data.id === game.playerId));
  for (const entry of ordered) {
    const group = groups.get(`${entry.tx},${entry.ty}`);
    let offX = 0;
    let offY = 0;
    if (group.length > 1) {
      const i = group.indexOf(entry);
      const a = (i / group.length) * Math.PI * 2 - Math.PI / 2;
      offX = Math.cos(a) * cs * 0.28;
      offY = Math.sin(a) * cs * 0.28;
    }
    drawCharacter(ctx, entry, X(entry.x + 0.5) + offX, Y(entry.y + 0.5) + offY, now);
  }

  // 拍照闪光
  if (game.flashAt) {
    const age = (now - game.flashAt) / 350;
    if (age >= 1) game.flashAt = 0;
    else {
      ctx.fillStyle = `rgba(255,255,255,${0.35 * (1 - age)})`;
      ctx.fillRect(0, 0, view.w, view.h);
    }
  }
}

function drawCharacter(ctx, entry, cx, cy, now) {
  const c = entry.data;
  const cs = view.cs;
  const isPlayer = c.id === game.playerId;
  const r = cs * (isPlayer ? 0.48 : 0.4);

  // 说话声波：服务端标记说话中，或本地正在播放其语音
  let intensity = 0;
  if (game.audioOut) intensity = game.audioOut.playingGain(c.id);
  if (isPlayer && game.talking) intensity = Math.max(intensity, 0.4 + Math.min(0.6, game.micLevel * 8));
  if (c.speaking) intensity = Math.max(intensity, 0.55);
  if (intensity > 0) {
    for (let i = 0; i < 3; i++) {
      const phase = ((now / 1100) + i / 3) % 1;
      ctx.strokeStyle = hexAlpha(entry.color, (1 - phase) * 0.75 * Math.min(1, intensity + 0.2));
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(cx, cy, r * (1.1 + phase * 1.5), 0, Math.PI * 2);
      ctx.stroke();
    }
  }

  if (isPlayer) {
    ctx.save();
    ctx.shadowColor = "rgba(255,209,102,0.85)";
    ctx.shadowBlur = cs * 0.8;
    ctx.fillStyle = entry.color;
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 2.5;
    ctx.stroke();
  } else {
    ctx.fillStyle = entry.color;
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = "rgba(10,13,19,0.9)";
    ctx.lineWidth = 1.5;
    ctx.stroke();
  }

  const nameSize = Math.max(11, Math.min(15, Math.round(cs * 0.6)));
  ctx.font = `600 ${nameSize}px ${FONT}`;
  const name = isPlayer ? `${c.name || "你"}（你）` : c.name || c.id;
  const nameY = cy + r + nameSize * 0.8;
  drawLabel(ctx, name, cx, nameY, isPlayer ? PLAYER_COLOR : "#eef2f8");
  if (c.activity) {
    const actSize = Math.max(10, nameSize - 2);
    ctx.font = `${actSize}px ${FONT}`;
    drawLabel(ctx, c.activity, cx, nameY + nameSize * 0.6 + actSize * 0.6, "#aab6c8");
  }
}

function hexAlpha(hex, alpha) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha.toFixed(3)})`;
}

function updateMicIndicator() {
  if (!game.talking) return;
  const level = Math.min(1, game.micLevel * 10);
  el.micIndicator.style.setProperty("--level", level.toFixed(2));
}

/* ------------------------------------------------------------------ 工具 */

function formatDuration(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  const m = Math.floor(s / 60);
  return `${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

function toast(text, kind = "info", ms = 3500) {
  const node = document.createElement("div");
  node.className = `toast toast-${kind}`;
  node.textContent = text;
  el.toasts.appendChild(node);
  while (el.toasts.childElementCount > 4) el.toasts.firstChild.remove();
  setTimeout(() => {
    node.classList.add("leaving");
    setTimeout(() => node.remove(), 400);
  }, ms);
}

/* ------------------------------------------------------------------ 启动 */

function bindEvents() {
  el.startBtn.addEventListener("click", start);
  el.photoBtn.addEventListener("click", () => {
    takePhoto();
    el.photoBtn.blur();
  });
  el.submitBtn.addEventListener("click", askSubmit);
  el.confirmOk.addEventListener("click", confirmSubmit);
  el.confirmCancel.addEventListener("click", () => el.confirmOverlay.classList.add("hidden"));
  el.reloadBtn.addEventListener("click", () => location.reload());
  el.sayForm.addEventListener("submit", sayText);
  el.canvas.addEventListener("click", onCanvasClick);
  el.canvas.addEventListener("mousemove", onCanvasMove);
  el.canvas.addEventListener("mouseleave", () => {
    game.hoverCell = null;
  });
  window.addEventListener("keydown", onKeyDown);
  window.addEventListener("keyup", onKeyUp);
  window.addEventListener("blur", () => {
    releaseDirs();
    stopTalking();
  });
  window.addEventListener("resize", resizeCanvas);
  new ResizeObserver(resizeCanvas).observe(el.stage);
}

bindEvents();
connect();
requestAnimationFrame(frame);
