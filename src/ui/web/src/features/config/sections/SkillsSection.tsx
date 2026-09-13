import { Card, Tag, Typography } from "antd";
import { AppstoreOutlined } from "@ant-design/icons";

const { Text } = Typography;

interface Props {
  skillsPool: Array<{ name: string; description?: string; source?: string }>;
  defaultInclude: string[] | undefined;
}

export function SkillsSection({ skillsPool, defaultInclude }: Props) {
  const safeDefaultInclude = defaultInclude || [];

  return (
    <Card
      title={
        <span>
          <AppstoreOutlined style={{ marginRight: 8 }} />
          技能（{skillsPool.length}）
        </span>
      }
    >
      {skillsPool.length === 0 ? (
        <Text type="secondary" style={{ fontSize: 13 }}>技能池中未发现技能</Text>
      ) : (
        skillsPool.map((skill) => {
          const isDefault = safeDefaultInclude.includes(skill.name);
          return (
            <div key={skill.name} style={{ padding: "4px 0", display: "flex", alignItems: "center", gap: 6, fontSize: 13 }}>
              <span style={{ fontWeight: 500 }}>{skill.name}</span>
              <Tag
                color={isDefault ? "blue" : undefined}
                style={{ fontSize: 11, lineHeight: "16px", padding: "0 4px" }}
              >
                {isDefault ? "default" : "deferred"}
              </Tag>
              {skill.description && (
                <span style={{ fontSize: 12, color: "var(--coara-text-faint)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {skill.description}
                </span>
              )}
            </div>
          );
        })
      )}
    </Card>
  );
}
