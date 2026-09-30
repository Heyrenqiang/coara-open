const m = window.__COARA_BASELINE__.React;
export const jsx = (type, props, key) => m.createElement(type, propsWithKey(props, key));
export const jsxs = jsx;
export const Fragment = m.Fragment;
function propsWithKey(props, key) {
  if (key === undefined || key === null) return props;
  return { ...props, key };
}
