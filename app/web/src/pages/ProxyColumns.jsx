import React, { useState } from "react";
import { Button } from "../ui";
const defaults = {
  order: [2, 3, 4, 5],
  visible: [2, 3, 4, 5],
  widths: { 2: 90, 3: 110, 4: 240, 5: 150 },
};
const names = { 2: "协议", 3: "状态", 4: "目标结果", 5: "检测时间" };
export function useProxyColumns() {
  const [config, setConfig] = useState(() => {
    try {
      const saved = JSON.parse(localStorage.getItem("proxy-column-layout"));
      if (
        saved &&
        Array.isArray(saved.order) &&
        saved.order.length === 4 &&
        new Set(saved.order).size === 4 &&
        saved.order.every((i) => names[i]) &&
        Array.isArray(saved.visible) &&
        saved.widths
      )
        return saved;
    } catch {}
    return defaults;
  });
  function update(next) {
    setConfig(next);
    try {
      localStorage.setItem("proxy-column-layout", JSON.stringify(next));
    } catch {}
  }
  const order = [
    0,
    1,
    ...config.order.filter((i) => config.visible.includes(i)),
    6,
  ];
  function arrange(values) {
    return order.map((i) => (
      <div
        key={i}
        className="proxy-cell"
        style={
          names[i]
            ? {
                width: Math.max(
                  64,
                  Math.min(600, Number(config.widths[i]) || defaults.widths[i]),
                ),
              }
            : undefined
        }
      >
        {values[i]}
      </div>
    ));
  }
  const settings = (
    <div className="proxy-form">
      {config.order.map((i, index) => (
        <div className="row wrap" key={i}>
          <label className="row">
            <input
              type="checkbox"
              checked={config.visible.includes(i)}
              onChange={(e) =>
                update({
                  ...config,
                  visible: e.target.checked
                    ? [...config.visible, i]
                    : config.visible.filter((v) => v !== i),
                })
              }
            />
            {names[i]}
          </label>
          <label>
            宽度（像素）
            <input
              aria-label={`${names[i]}列宽`}
              type="number"
              min="64"
              max="600"
              value={config.widths[i] || defaults.widths[i]}
              onChange={(e) =>
                update({
                  ...config,
                  widths: { ...config.widths, [i]: Number(e.target.value) },
                })
              }
            />
          </label>
          <Button
            disabled={index === 0}
            onClick={() => {
              const next = [...config.order];
              [next[index - 1], next[index]] = [next[index], next[index - 1]];
              update({ ...config, order: next });
            }}
          >
            左移
          </Button>
          <Button
            disabled={index === config.order.length - 1}
            onClick={() => {
              const next = [...config.order];
              [next[index + 1], next[index]] = [next[index], next[index + 1]];
              update({ ...config, order: next });
            }}
          >
            右移
          </Button>
        </div>
      ))}
      <Button onClick={() => update(defaults)}>恢复默认列</Button>
      <small>
        选择、节点和操作列固定；其他列的显隐、顺序和宽度保存在此浏览器。
      </small>
    </div>
  );
  return { arrange, settings };
}
