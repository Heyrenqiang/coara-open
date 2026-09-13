import { Tag, Tooltip } from "antd";

const EXPERIMENTAL_HINT =
  "该模块仍处于实验阶段，功能与交互会随迭代调整，可能出现变化；稳定流程请谨慎使用。";

/**
 * 可复用的「实验」标识：小号中性 Tag + 悬停说明。
 * 给仍在迭代中的产品模块打标签，让用户一眼知道该功能尚未定型。
 */
export function ExperimentalBadge() {
  return (
    <Tooltip title={EXPERIMENTAL_HINT}>
      <Tag
        color="default"
        bordered
        style={{
          marginInlineEnd: 0,
          fontSize: 11,
          lineHeight: "18px",
          padding: "0 6px",
          fontWeight: 500,
        }}
      >
        实验
      </Tag>
    </Tooltip>
  );
}
