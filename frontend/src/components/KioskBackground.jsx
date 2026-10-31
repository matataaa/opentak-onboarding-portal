import { useEffect, useRef } from 'react';
import './KioskBackground.css';

const FRAME_MS = 1000 / 30;
const MAX_DT_S = 0.1;
const MAX_DPR = 1.5;
const RESIZE_DEBOUNCE_MS = 200;

const AREA_PER_NODE = 30000;
const MIN_NODES = 12;
const MAX_NODES = 60;
const MIN_NODE_SPEED = 4; // px/s
const MAX_NODE_SPEED = 10;
const NODE_RADIUS = 2;
const NODE_ALPHA = 0.6;

const LINK_ALPHAS = [0.14, 0.24, 0.36, 0.5]; // Far to near

const PULSE_POOL_SIZE = 4;
const PULSE_SPEED = 60; // px/s
const MIN_PULSE_GAP_S = 1;
const MAX_PULSE_GAP_S = 4;
const PULSE_RADIUS = 2.5;
const PULSE_HALO_RADIUS = 5;
const PULSE_HALO_ALPHA = 0.25;

const REDUCED_MOTION_QUERY = '(prefers-reduced-motion: reduce)';

const randomBetween = (min, max) => min + Math.random() * (max - min);

function KioskBackground() {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const reducedMotion = window.matchMedia(REDUCED_MOTION_QUERY);
    const colors = { node: '', link: '', pulse: '' };
    const nodes = [];
    const linkBuckets = LINK_ALPHAS.map(() => []); // Flat [a, b, a, b, ...] node indices
    const pulses = Array.from({ length: PULSE_POOL_SIZE }, () => ({ active: false, a: 0, b: 0, t: 0 }));

    let width = 0;
    let height = 0;
    let canvasWidth = 0;
    let canvasHeight = 0;
    let linkDistance = 0;
    let nextPulseIn = 0;
    let rafId = null;
    let lastFrame = 0;
    let resizeTimer = null;

    const readColors = () => {
      const style = getComputedStyle(document.documentElement);
      colors.node = style.getPropertyValue('--kiosk-mesh-node').trim();
      colors.link = style.getPropertyValue('--kiosk-mesh-link').trim();
      colors.pulse = style.getPropertyValue('--kiosk-mesh-pulse').trim();
    };

    const clearPulses = () => {
      pulses.forEach((pulse) => { pulse.active = false; });
    };

    const createNode = () => {
      const angle = Math.random() * Math.PI * 2;
      const speed = randomBetween(MIN_NODE_SPEED, MAX_NODE_SPEED);
      return {
        x: Math.random() * width,
        y: Math.random() * height,
        vx: Math.cos(angle) * speed,
        vy: Math.sin(angle) * speed,
      };
    };

    const applySize = () => {
      const newWidth = canvas.clientWidth;
      const newHeight = canvas.clientHeight;
      const dpr = Math.min(window.devicePixelRatio || 1, MAX_DPR);
      const newCanvasWidth = Math.round(newWidth * dpr);
      const newCanvasHeight = Math.round(newHeight * dpr);
      // Setting canvas.width reallocates the backing store, so skip no-op resizes
      if (newCanvasWidth === canvasWidth && newCanvasHeight === canvasHeight) return;

      const scaleX = width ? newWidth / width : 1;
      const scaleY = height ? newHeight / height : 1;
      width = newWidth;
      height = newHeight;
      canvasWidth = newCanvasWidth;
      canvasHeight = newCanvasHeight;
      canvas.width = canvasWidth;
      canvas.height = canvasHeight;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      const area = width * height;
      const count = area ? Math.min(MAX_NODES, Math.max(MIN_NODES, Math.round(area / AREA_PER_NODE))) : 0;
      // Scaling with the node count keeps the mesh equally connected on any screen
      linkDistance = count ? Math.sqrt(area / count) : 0;
      nodes.forEach((node) => {
        node.x *= scaleX;
        node.y *= scaleY;
      });
      nodes.length = Math.min(nodes.length, count);
      while (nodes.length < count) nodes.push(createNode());
      clearPulses();
    };

    const spawnPulse = () => {
      const pulse = pulses.find((candidate) => !candidate.active);
      if (!pulse) return;
      const a = Math.floor(Math.random() * nodes.length);
      const offset = Math.floor(Math.random() * nodes.length);
      for (let i = 0; i < nodes.length; i++) {
        const b = (offset + i) % nodes.length;
        if (b !== a && Math.hypot(nodes[b].x - nodes[a].x, nodes[b].y - nodes[a].y) < linkDistance) {
          pulse.active = true;
          pulse.a = a;
          pulse.b = b;
          pulse.t = 0;
          return;
        }
      }
    };

    const step = (dt) => {
      nodes.forEach((node) => {
        node.x += node.vx * dt;
        node.y += node.vy * dt;
        if (node.x < 0) {
          node.x = 0;
          node.vx = Math.abs(node.vx);
        } else if (node.x > width) {
          node.x = width;
          node.vx = -Math.abs(node.vx);
        }
        if (node.y < 0) {
          node.y = 0;
          node.vy = Math.abs(node.vy);
        } else if (node.y > height) {
          node.y = height;
          node.vy = -Math.abs(node.vy);
        }
      });

      pulses.forEach((pulse) => {
        if (!pulse.active) return;
        const a = nodes[pulse.a];
        const b = nodes[pulse.b];
        const distance = Math.hypot(b.x - a.x, b.y - a.y);
        pulse.t += (PULSE_SPEED * dt) / distance;
        // A pulse also ends when its link breaks
        if (distance >= linkDistance || pulse.t >= 1) pulse.active = false;
      });

      nextPulseIn -= dt;
      if (nextPulseIn <= 0) {
        nextPulseIn = randomBetween(MIN_PULSE_GAP_S, MAX_PULSE_GAP_S);
        spawnPulse();
      }
    };

    const fillPulses = (radius) => {
      ctx.beginPath();
      pulses.forEach((pulse) => {
        if (!pulse.active) return;
        const a = nodes[pulse.a];
        const b = nodes[pulse.b];
        const x = a.x + (b.x - a.x) * pulse.t;
        const y = a.y + (b.y - a.y) * pulse.t;
        ctx.moveTo(x + radius, y);
        ctx.arc(x, y, radius, 0, Math.PI * 2);
      });
      ctx.fill();
    };

    const draw = () => {
      ctx.clearRect(0, 0, width, height);

      linkBuckets.forEach((bucket) => { bucket.length = 0; });
      const maxDistanceSq = linkDistance * linkDistance;
      for (let a = 0; a < nodes.length; a++) {
        for (let b = a + 1; b < nodes.length; b++) {
          const dx = nodes[a].x - nodes[b].x;
          const dy = nodes[a].y - nodes[b].y;
          const distanceSq = dx * dx + dy * dy;
          if (distanceSq < maxDistanceSq) {
            const closeness = 1 - Math.sqrt(distanceSq) / linkDistance;
            linkBuckets[Math.min(LINK_ALPHAS.length - 1, Math.floor(closeness * LINK_ALPHAS.length))].push(a, b);
          }
        }
      }

      ctx.strokeStyle = colors.link;
      ctx.lineWidth = 1;
      linkBuckets.forEach((bucket, index) => {
        ctx.globalAlpha = LINK_ALPHAS[index];
        ctx.beginPath();
        for (let i = 0; i < bucket.length; i += 2) {
          const a = nodes[bucket[i]];
          const b = nodes[bucket[i + 1]];
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
        }
        ctx.stroke();
      });

      ctx.fillStyle = colors.node;
      ctx.globalAlpha = NODE_ALPHA;
      ctx.beginPath();
      nodes.forEach((node) => {
        ctx.moveTo(node.x + NODE_RADIUS, node.y);
        ctx.arc(node.x, node.y, NODE_RADIUS, 0, Math.PI * 2);
      });
      ctx.fill();

      ctx.fillStyle = colors.pulse;
      ctx.globalAlpha = PULSE_HALO_ALPHA;
      fillPulses(PULSE_HALO_RADIUS);
      ctx.globalAlpha = 1;
      fillPulses(PULSE_RADIUS);
    };

    const tick = (now) => {
      rafId = requestAnimationFrame(tick);
      const elapsed = now - lastFrame;
      // The tolerance keeps a 60Hz display on every second frame despite timer jitter
      if (elapsed < FRAME_MS - 1) return;
      lastFrame = now;
      step(Math.min(elapsed / 1000, MAX_DT_S));
      draw();
    };

    const syncAnimation = () => {
      if (reducedMotion.matches || document.hidden) {
        if (rafId !== null) cancelAnimationFrame(rafId);
        rafId = null;
      } else if (rafId === null) {
        lastFrame = performance.now();
        rafId = requestAnimationFrame(tick);
      }
    };

    const handleReducedMotionChange = () => {
      if (reducedMotion.matches) clearPulses();
      syncAnimation();
      draw();
    };

    const handleResize = () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        applySize();
        draw();
      }, RESIZE_DEBOUNCE_MS);
    };

    const resizeObserver = new ResizeObserver(handleResize);
    // ThemeContext can rewrite data-theme after mount
    const themeObserver = new MutationObserver(() => {
      readColors();
      draw();
    });

    readColors();
    applySize();
    draw();
    syncAnimation();

    resizeObserver.observe(canvas);
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    document.addEventListener('visibilitychange', syncAnimation);
    reducedMotion.addEventListener('change', handleReducedMotionChange);

    return () => {
      if (rafId !== null) cancelAnimationFrame(rafId);
      clearTimeout(resizeTimer);
      resizeObserver.disconnect();
      themeObserver.disconnect();
      document.removeEventListener('visibilitychange', syncAnimation);
      reducedMotion.removeEventListener('change', handleReducedMotionChange);
    };
  }, []);

  return <canvas ref={canvasRef} className="kiosk-background" aria-hidden="true" />;
}

export default KioskBackground;
