/** 整页文件显示页的路由构造（/file?path=<urlencoded>）。 */
export function fileViewRoute(path: string): string {
  return `/file?path=${encodeURIComponent(path)}`;
}
