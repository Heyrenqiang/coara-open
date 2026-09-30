// 插件构建垫片：react → 壳冻结基线（window.__COARA_BASELINE__）。
// 插件 vite 构建把这些裸标识符 alias 到本目录垫片，产物里不出现裸 import，
// 运行时从全局取壳的共享实例（同一份 React 单例，hooks/上下文才一致）。
const m = window.__COARA_BASELINE__.React;
export default m;
export const Children = m.Children;
export const Component = m.Component;
export const PureComponent = m.PureComponent;
export const StrictMode = m.StrictMode;
export const Suspense = m.Suspense;
export const Fragment = m.Fragment;
export const cloneElement = m.cloneElement;
export const createContext = m.createContext;
export const createElement = m.createElement;
export const createRef = m.createRef;
export const forwardRef = m.forwardRef;
export const isValidElement = m.isValidElement;
export const lazy = m.lazy;
export const memo = m.memo;
export const startTransition = m.startTransition;
export const useCallback = m.useCallback;
export const useContext = m.useContext;
export const useDeferredValue = m.useDeferredValue;
export const useEffect = m.useEffect;
export const useId = m.useId;
export const useImperativeHandle = m.useImperativeHandle;
export const useLayoutEffect = m.useLayoutEffect;
export const useMemo = m.useMemo;
export const useReducer = m.useReducer;
export const useRef = m.useRef;
export const useState = m.useState;
export const useSyncExternalStore = m.useSyncExternalStore;
export const useTransition = m.useTransition;
