/// <reference types="vite/client" />

interface Window {
  /** 由服务端注入的技能池（ConfigPanel 启动时读取；未注入时视为空）。 */
  __COARA_SKILLS_POOL__?: Array<{
    name: string;
    description?: string;
    source?: string;
  }>;
}
