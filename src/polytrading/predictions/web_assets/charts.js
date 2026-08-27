const SVG_NAMESPACE = "http://www.w3.org/2000/svg";
let chartSequence = 0;

function resolveDocument(documentRef) {
  const resolved = documentRef ?? globalThis.document;
  if (!resolved || typeof resolved.createElementNS !== "function") {
    throw new TypeError("a DOM document is required");
  }
  return resolved;
}

function fallbackNode(documentRef, text) {
  const node = documentRef.createElement("p");
  node.className = "chart-fallback";
  node.textContent = text;
  return node;
}

function finiteSeries(values) {
  return (
    Array.isArray(values) &&
    values.length > 0 &&
    values.every((value) => typeof value === "number" && Number.isFinite(value))
  );
}

function svgNode(documentRef, name, attributes = {}) {
  const node = documentRef.createElementNS(SVG_NAMESPACE, name);
  for (const [attribute, value] of Object.entries(attributes)) {
    node.setAttribute(attribute, value);
  }
  return node;
}

function accessibleSvg(documentRef, { title, description, width, height, className }) {
  chartSequence += 1;
  const titleId = `atlas-chart-title-${chartSequence}`;
  const descriptionId = `atlas-chart-description-${chartSequence}`;
  const svg = svgNode(documentRef, "svg", {
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-labelledby": `${titleId} ${descriptionId}`,
    focusable: "false",
    preserveAspectRatio: "none",
  });
  svg.setAttribute("class", className);
  const titleNode = svgNode(documentRef, "title", { id: titleId });
  titleNode.textContent = title;
  const descriptionNode = svgNode(documentRef, "desc", { id: descriptionId });
  descriptionNode.textContent = description;
  svg.append(titleNode, descriptionNode);
  return svg;
}

function boundedCoordinate(value, minimum, maximum, start, extent) {
  if (maximum === minimum) {
    return start + extent / 2;
  }
  const scale = Math.max(1, Math.abs(minimum), Math.abs(maximum));
  const scaledMinimum = minimum / scale;
  const scaledMaximum = maximum / scale;
  const scaledValue = value / scale;
  const ratio = Math.min(
    1,
    Math.max(0, (scaledValue - scaledMinimum) / (scaledMaximum - scaledMinimum)),
  );
  return start + ratio * extent;
}

export function sparklineSvg(values, {
  documentRef,
  title = "Observed trend",
  description = "A bounded sequence of observed values.",
  unavailableText = "Trend unavailable for this snapshot.",
  width = 320,
  height = 88,
} = {}) {
  const dom = resolveDocument(documentRef);
  if (!finiteSeries(values)) {
    return fallbackNode(dom, unavailableText);
  }
  const safeWidth = Math.min(1000, Math.max(80, Number(width) || 320));
  const safeHeight = Math.min(400, Math.max(40, Number(height) || 88));
  const inset = 7;
  const minimum = Math.min(...values);
  const maximum = Math.max(...values);
  const points = values.map((value, index) => {
    const x = values.length === 1
      ? safeWidth / 2
      : inset + (index / (values.length - 1)) * (safeWidth - inset * 2);
    const y = boundedCoordinate(
      value,
      minimum,
      maximum,
      safeHeight - inset,
      -(safeHeight - inset * 2),
    );
    return [x, y];
  });
  const pathData = points
    .map(([x, y], index) => `${index === 0 ? "M" : "L"} ${x.toFixed(2)} ${y.toFixed(2)}`)
    .join(" ");
  const svg = accessibleSvg(dom, {
    title,
    description,
    width: safeWidth,
    height: safeHeight,
    className: "chart chart--sparkline",
  });
  const baseline = svgNode(dom, "line", {
    x1: inset,
    x2: safeWidth - inset,
    y1: safeHeight - inset,
    y2: safeHeight - inset,
    stroke: "currentColor",
    "stroke-opacity": "0.16",
    "vector-effect": "non-scaling-stroke",
  });
  const path = svgNode(dom, "path", {
    d: pathData,
    fill: "none",
    stroke: "currentColor",
    "stroke-width": "2",
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    "vector-effect": "non-scaling-stroke",
  });
  svg.append(baseline, path);
  return svg;
}

export function depthBarsSvg(values, {
  documentRef,
  title = "Observed depth",
  description = "Relative bars for available observed values.",
  unavailableText = "Depth unavailable for this snapshot.",
  width = 320,
  height = 96,
} = {}) {
  const dom = resolveDocument(documentRef);
  if (!finiteSeries(values) || values.some((value) => value < 0)) {
    return fallbackNode(dom, unavailableText);
  }
  const safeWidth = Math.min(1000, Math.max(80, Number(width) || 320));
  const safeHeight = Math.min(400, Math.max(40, Number(height) || 96));
  const maximum = Math.max(...values);
  const gap = 4;
  const barWidth = Math.max(1, (safeWidth - gap * (values.length - 1)) / values.length);
  const svg = accessibleSvg(dom, {
    title,
    description,
    width: safeWidth,
    height: safeHeight,
    className: "chart chart--bars",
  });
  values.forEach((value, index) => {
    const ratio = maximum === 0 ? 0 : Math.min(1, Math.max(0, value / maximum));
    const barHeight = Math.max(2, ratio * (safeHeight - 8));
    svg.append(
      svgNode(dom, "rect", {
        x: index * (barWidth + gap),
        y: safeHeight - barHeight,
        width: barWidth,
        height: barHeight,
        rx: "2",
        fill: "currentColor",
        opacity: String(0.38 + ratio * 0.58),
      }),
    );
  });
  return svg;
}

export function freshnessArcSvg(ageSeconds, maximumAgeSeconds, {
  documentRef,
  title = "Snapshot freshness",
  description = "Observed age relative to the freshness threshold.",
  unavailableText = "Freshness unavailable for this snapshot.",
  size = 96,
} = {}) {
  const dom = resolveDocument(documentRef);
  if (
    typeof ageSeconds !== "number" ||
    !Number.isFinite(ageSeconds) ||
    ageSeconds < 0 ||
    typeof maximumAgeSeconds !== "number" ||
    !Number.isFinite(maximumAgeSeconds) ||
    maximumAgeSeconds <= 0
  ) {
    return fallbackNode(dom, unavailableText);
  }
  const safeSize = Math.min(240, Math.max(56, Number(size) || 96));
  const radius = safeSize * 0.38;
  const circumference = 2 * Math.PI * radius;
  const freshness = 1 - Math.min(1, ageSeconds / maximumAgeSeconds);
  const svg = accessibleSvg(dom, {
    title,
    description,
    width: safeSize,
    height: safeSize,
    className: "chart chart--freshness",
  });
  const track = svgNode(dom, "circle", {
    class: "chart__track",
    cx: safeSize / 2,
    cy: safeSize / 2,
    r: radius,
    fill: "none",
    stroke: "currentColor",
    "stroke-width": "7",
    "stroke-opacity": "0.22",
  });
  const arc = svgNode(dom, "circle", {
    cx: safeSize / 2,
    cy: safeSize / 2,
    r: radius,
    fill: "none",
    stroke: "currentColor",
    "stroke-width": "7",
    "stroke-linecap": "round",
    "stroke-dasharray": `${(circumference * freshness).toFixed(2)} ${circumference.toFixed(2)}`,
    transform: `rotate(-90 ${safeSize / 2} ${safeSize / 2})`,
  });
  const label = svgNode(dom, "text", {
    x: "50%",
    y: "52%",
    fill: "currentColor",
    "text-anchor": "middle",
    "dominant-baseline": "middle",
  });
  label.textContent = `${Math.round(ageSeconds)}s`;
  svg.append(track, arc, label);
  return svg;
}
