const canvas = document.getElementById("game");
const ctx = canvas.getContext("2d");

const scoreNode = document.getElementById("score");
const bestNode = document.getElementById("best-score");
const stateNode = document.getElementById("state-label");
const overlay = document.getElementById("overlay");
const overlayTitle = document.getElementById("overlay-title");
const overlayText = document.getElementById("overlay-text");

const config = {
  width: canvas.width,
  height: canvas.height,
  gravity: 0.42,
  flapVelocity: -7.3,
  scrollSpeed: 2.8,
  pipeWidth: 82,
  pipeGap: 176,
  pipeSpacing: 250,
  groundHeight: 102,
  playerX: 126,
  playerRadius: 20
};

const storageKey = "sky-gate-best-score";

const state = {
  mode: "ready",
  score: 0,
  best: Number(localStorage.getItem(storageKey) || 0),
  player: {
    x: config.playerX,
    y: config.height / 2 - 20,
    velocity: 0,
    rotation: 0
  },
  pipes: [],
  clouds: [
    { x: 90, y: 100, size: 34, speed: 0.22 },
    { x: 300, y: 160, size: 22, speed: 0.28 },
    { x: 210, y: 250, size: 28, speed: 0.16 }
  ],
  lastTime: 0
};

bestNode.textContent = String(state.best);

function resetRound() {
  state.mode = "ready";
  state.score = 0;
  state.player.y = config.height / 2 - 20;
  state.player.velocity = 0;
  state.player.rotation = 0;
  state.pipes = [];
  updateHud();
  showOverlay("準備起飛", "按下空白鍵穿越能量門。");
}

function startRound() {
  if (state.mode === "running") {
    flap();
    return;
  }

  if (state.mode === "gameover" || state.mode === "ready") {
    state.mode = "running";
    state.score = 0;
    state.player.y = config.height / 2 - 20;
    state.player.velocity = config.flapVelocity;
    state.player.rotation = -0.3;
    state.pipes = createInitialPipes();
    updateHud();
    hideOverlay();
  }
}

function endRound() {
  state.mode = "gameover";
  if (state.score > state.best) {
    state.best = state.score;
    localStorage.setItem(storageKey, String(state.best));
    bestNode.textContent = String(state.best);
  }
  updateHud();
  showOverlay("任務失敗", "按下空白鍵立即重新開始。");
}

function flap() {
  if (state.mode !== "running") {
    return;
  }
  state.player.velocity = config.flapVelocity;
  state.player.rotation = -0.48;
}

function createPipe(x) {
  const ceiling = 90;
  const floor = config.height - config.groundHeight - 110 - config.pipeGap;
  const gapY = ceiling + Math.random() * Math.max(20, floor - ceiling);
  return {
    x,
    gapY,
    scored: false
  };
}

function createInitialPipes() {
  return [
    createPipe(config.width + 120),
    createPipe(config.width + 120 + config.pipeSpacing),
    createPipe(config.width + 120 + config.pipeSpacing * 2)
  ];
}

function updateHud() {
  scoreNode.textContent = String(state.score);
  bestNode.textContent = String(state.best);
  stateNode.textContent =
    state.mode === "running" ? "Running" :
    state.mode === "gameover" ? "Crashed" :
    "Ready";
}

function showOverlay(title, text) {
  overlayTitle.textContent = title;
  overlayText.textContent = text;
  overlay.classList.remove("hidden");
}

function hideOverlay() {
  overlay.classList.add("hidden");
}

function update(delta) {
  updateClouds(delta);

  if (state.mode !== "running") {
    return;
  }

  state.player.velocity += config.gravity * delta;
  state.player.y += state.player.velocity * delta * 1.25;
  state.player.rotation = Math.min(1.2, state.player.rotation + 0.045 * delta);

  updatePipes(delta);
  checkCollisions();
}

function updateClouds(delta) {
  state.clouds.forEach((cloud) => {
    cloud.x -= cloud.speed * delta;
    if (cloud.x < -80) {
      cloud.x = config.width + 40;
    }
  });
}

function updatePipes(delta) {
  for (const pipe of state.pipes) {
    pipe.x -= config.scrollSpeed * delta;

    if (!pipe.scored && pipe.x + config.pipeWidth < state.player.x) {
      pipe.scored = true;
      state.score += 1;
      updateHud();
    }
  }

  const lastPipe = state.pipes[state.pipes.length - 1];
  if (lastPipe && lastPipe.x < config.width - config.pipeSpacing) {
    state.pipes.push(createPipe(lastPipe.x + config.pipeSpacing));
  }

  state.pipes = state.pipes.filter((pipe) => pipe.x + config.pipeWidth > -40);
}

function checkCollisions() {
  const playerTop = state.player.y - config.playerRadius;
  const playerBottom = state.player.y + config.playerRadius;
  const groundTop = config.height - config.groundHeight;

  if (playerTop <= 0 || playerBottom >= groundTop) {
    endRound();
    return;
  }

  for (const pipe of state.pipes) {
    const withinPipeX =
      state.player.x + config.playerRadius > pipe.x &&
      state.player.x - config.playerRadius < pipe.x + config.pipeWidth;

    if (!withinPipeX) {
      continue;
    }

    const hitsTop = playerTop < pipe.gapY;
    const hitsBottom = playerBottom > pipe.gapY + config.pipeGap;

    if (hitsTop || hitsBottom) {
      endRound();
      return;
    }
  }
}

function draw() {
  drawSky();
  drawSun();
  drawClouds();
  drawPipes();
  drawGround();
  drawPlayer();
  drawScoreStamp();
}

function drawSky() {
  ctx.clearRect(0, 0, config.width, config.height);

  const gradient = ctx.createLinearGradient(0, 0, 0, config.height);
  gradient.addColorStop(0, "#72d8ff");
  gradient.addColorStop(0.55, "#dbf6ff");
  gradient.addColorStop(1, "#f4dfb6");
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, config.width, config.height);
}

function drawSun() {
  ctx.save();
  ctx.fillStyle = "rgba(255, 209, 102, 0.28)";
  ctx.beginPath();
  ctx.arc(330, 110, 62, 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();
}

function drawClouds() {
  ctx.save();
  ctx.fillStyle = "rgba(255, 255, 255, 0.78)";
  for (const cloud of state.clouds) {
    ctx.beginPath();
    ctx.arc(cloud.x, cloud.y, cloud.size, 0, Math.PI * 2);
    ctx.arc(cloud.x + cloud.size * 0.8, cloud.y + 8, cloud.size * 0.8, 0, Math.PI * 2);
    ctx.arc(cloud.x - cloud.size * 0.8, cloud.y + 10, cloud.size * 0.7, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.restore();
}

function drawPipes() {
  for (const pipe of state.pipes) {
    drawSinglePipe(pipe.x, 0, pipe.gapY, true);
    drawSinglePipe(
      pipe.x,
      pipe.gapY + config.pipeGap,
      config.height - config.groundHeight - (pipe.gapY + config.pipeGap),
      false
    );
  }
}

function drawSinglePipe(x, y, height, topPipe) {
  const bodyGradient = ctx.createLinearGradient(x, y, x + config.pipeWidth, y);
  bodyGradient.addColorStop(0, "#0c5c7f");
  bodyGradient.addColorStop(0.5, "#18a3c7");
  bodyGradient.addColorStop(1, "#0e6a8a");

  ctx.fillStyle = bodyGradient;
  ctx.fillRect(x, y, config.pipeWidth, height);

  ctx.fillStyle = "#7ce2ff";
  ctx.fillRect(x - 5, topPipe ? height - 14 : y, config.pipeWidth + 10, 14);

  ctx.strokeStyle = "rgba(255,255,255,0.24)";
  ctx.lineWidth = 2;
  ctx.strokeRect(x, y, config.pipeWidth, height);
}

function drawGround() {
  const y = config.height - config.groundHeight;
  const groundGradient = ctx.createLinearGradient(0, y, 0, config.height);
  groundGradient.addColorStop(0, "#8e6a38");
  groundGradient.addColorStop(1, "#56381e");
  ctx.fillStyle = groundGradient;
  ctx.fillRect(0, y, config.width, config.groundHeight);

  ctx.fillStyle = "rgba(255, 209, 102, 0.24)";
  for (let i = 0; i < config.width; i += 28) {
    ctx.fillRect(i, y + 14, 18, 6);
  }
}

function drawPlayer() {
  ctx.save();
  ctx.translate(state.player.x, state.player.y);
  ctx.rotate(state.player.rotation);

  ctx.fillStyle = "#ff8c42";
  ctx.beginPath();
  ctx.ellipse(0, 0, 24, 20, 0, 0, Math.PI * 2);
  ctx.fill();

  ctx.fillStyle = "#ffd166";
  ctx.beginPath();
  ctx.arc(-6, -2, 8, 0, Math.PI * 2);
  ctx.fill();

  ctx.fillStyle = "#13293d";
  ctx.beginPath();
  ctx.arc(8, -5, 3, 0, Math.PI * 2);
  ctx.fill();

  ctx.fillStyle = "#ff6b6b";
  ctx.beginPath();
  ctx.moveTo(18, 0);
  ctx.lineTo(32, 5);
  ctx.lineTo(18, 10);
  ctx.closePath();
  ctx.fill();

  ctx.fillStyle = "#f94144";
  ctx.beginPath();
  ctx.moveTo(-12, 2);
  ctx.lineTo(-28, -4);
  ctx.lineTo(-26, 12);
  ctx.closePath();
  ctx.fill();

  ctx.restore();
}

function drawScoreStamp() {
  ctx.save();
  ctx.fillStyle = "rgba(5, 14, 24, 0.36)";
  ctx.fillRect(16, 18, 112, 48);
  ctx.strokeStyle = "rgba(255, 255, 255, 0.12)";
  ctx.strokeRect(16, 18, 112, 48);

  ctx.fillStyle = "#eaf7ff";
  ctx.font = "700 26px Segoe UI";
  ctx.fillText(String(state.score), 30, 52);
  ctx.restore();
}

function loop(timestamp) {
  if (!state.lastTime) {
    state.lastTime = timestamp;
  }

  const delta = Math.min(1.5, (timestamp - state.lastTime) / 16.666);
  state.lastTime = timestamp;

  update(delta);
  draw();
  requestAnimationFrame(loop);
}

window.addEventListener("keydown", (event) => {
  if (event.code !== "Space") {
    return;
  }

  event.preventDefault();

  if (state.mode === "ready" || state.mode === "gameover") {
    startRound();
    return;
  }

  flap();
});

canvas.addEventListener("pointerdown", () => {
  if (state.mode === "ready" || state.mode === "gameover") {
    startRound();
    return;
  }

  flap();
});

resetRound();
requestAnimationFrame(loop);
