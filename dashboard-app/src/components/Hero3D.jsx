import { useEffect, useRef } from "react";
import * as THREE from "three";

/**
 * Wireframe icosahedron with an inner core and a sparse point field.
 * Reads its colors from CSS custom properties so it follows the theme,
 * degrades to nothing when WebGL is unavailable, honors reduced motion,
 * and disposes every GPU resource on unmount.
 */
export default function Hero3D({ className, style }) {
  const containerRef = useRef(null);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return undefined;

    let renderer;
    let scene;
    let camera;
    let frameId;
    let resizeObserver;
    let themeObserver;
    let disposed = false;
    let icosahedron;
    let core;
    let points;

    const reduceMotion =
      window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    const accent = cssColor("--primary", "#c8f24e");
    const foreground = cssColor("--foreground", "#f2f3ed");
    const muted = cssColor("--muted-foreground", "#9ca093");

    try {
      const width = container.clientWidth || 1;
      const height = container.clientHeight || 1;

      scene = new THREE.Scene();
      camera = new THREE.PerspectiveCamera(45, width / height, 0.1, 100);
      camera.position.set(0, 0, 7);

      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
      renderer.setSize(width, height);
      container.appendChild(renderer.domElement);

      const icoGeo = new THREE.IcosahedronGeometry(2.1, 1);
      const icoMat = new THREE.MeshBasicMaterial({
        color: new THREE.Color(accent),
        wireframe: true,
        transparent: true,
        opacity: 0.32,
      });
      icosahedron = new THREE.Mesh(icoGeo, icoMat);
      scene.add(icosahedron);

      const coreGeo = new THREE.IcosahedronGeometry(0.5, 1);
      const coreMat = new THREE.MeshBasicMaterial({
        color: new THREE.Color(foreground),
        transparent: true,
        opacity: 0.9,
      });
      core = new THREE.Mesh(coreGeo, coreMat);
      scene.add(core);

      const pointCount = 110;
      const positions = new Float32Array(pointCount * 3);
      for (let i = 0; i < pointCount; i += 1) {
        const radius = 2.8 + Math.random() * 1.4;
        const theta = Math.random() * Math.PI * 2;
        const phi = Math.acos(2 * Math.random() - 1);
        positions[i * 3] = radius * Math.sin(phi) * Math.cos(theta);
        positions[i * 3 + 1] = radius * Math.sin(phi) * Math.sin(theta);
        positions[i * 3 + 2] = radius * Math.cos(phi);
      }
      const pointsGeo = new THREE.BufferGeometry();
      pointsGeo.setAttribute("position", new THREE.BufferAttribute(positions, 3));
      const pointsMat = new THREE.PointsMaterial({
        color: new THREE.Color(muted),
        size: 0.045,
        transparent: true,
        opacity: 0.65,
      });
      points = new THREE.Points(pointsGeo, pointsMat);
      scene.add(points);

      // The colors above are read once at mount, but the theme class is
      // applied by the provider after children mount and can be toggled
      // at any time — re-resolve the tokens whenever it changes, or the
      // orb keeps the previous theme's colors (white orb on light theme).
      const applyColors = () => {
        icoMat.color.set(cssColor("--primary", "#c8f24e"));
        coreMat.color.set(cssColor("--foreground", "#f2f3ed"));
        pointsMat.color.set(cssColor("--muted-foreground", "#9ca093"));
      };
      themeObserver = new MutationObserver(applyColors);
      themeObserver.observe(document.documentElement, {
        attributes: true,
        attributeFilter: ["class"],
      });

      const clock = new THREE.Clock();

      const renderFrame = () => {
        const t = clock.getElapsedTime();
        icosahedron.rotation.y = t * 0.12;
        icosahedron.rotation.x = t * 0.05;
        core.rotation.y = -t * 0.2;
        points.rotation.y = t * 0.04;
        renderer.render(scene, camera);
      };

      if (reduceMotion) {
        renderFrame();
      } else {
        const animate = () => {
          if (disposed) return;
          renderFrame();
          frameId = requestAnimationFrame(animate);
        };
        animate();
      }

      resizeObserver = new ResizeObserver(() => {
        if (!container || disposed) return;
        const w = container.clientWidth || 1;
        const h = container.clientHeight || 1;
        camera.aspect = w / h;
        camera.updateProjectionMatrix();
        renderer.setSize(w, h);
        if (reduceMotion) renderFrame();
      });
      resizeObserver.observe(container);
    } catch {
      // WebGL unavailable — the surrounding layout stands on its own.
    }

    return () => {
      disposed = true;
      if (frameId) cancelAnimationFrame(frameId);
      if (resizeObserver) resizeObserver.disconnect();
      if (themeObserver) themeObserver.disconnect();
      [icosahedron, core, points].forEach((object) => {
        if (!object) return;
        if (object.geometry) object.geometry.dispose();
        if (object.material) object.material.dispose();
      });
      if (renderer) {
        renderer.dispose();
        if (renderer.domElement && renderer.domElement.parentNode === container) {
          container.removeChild(renderer.domElement);
        }
      }
    };
  }, []);

  return <div ref={containerRef} className={className} style={{ width: "100%", height: "100%", ...style }} />;
}

/**
 * Resolve a CSS custom property to something THREE.Color can parse.
 * The design tokens are oklch(), which Three.js doesn't understand, so
 * we let the canvas 2D parser normalize it to rgb().
 */
function cssColor(name, fallback) {
  if (typeof window === "undefined") return fallback;
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  if (!value) return fallback;
  try {
    const ctx = document.createElement("canvas").getContext("2d");
    if (!ctx) return fallback;
    ctx.fillStyle = value;
    return ctx.fillStyle || fallback;
  } catch {
    return fallback;
  }
}
