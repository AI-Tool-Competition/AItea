import { NextResponse, type NextRequest } from "next/server";

/** The retained DeerFlow source is a component base, not a second public backend. */
export function proxy(request: NextRequest) {
  const path = request.nextUrl.pathname;
  if (path === "/api/tea" || path.startsWith("/api/tea/"))
    return NextResponse.next();
  if (path.startsWith("/api/"))
    return NextResponse.json(
      { error: { code: "not_found", message: "接口不存在" } },
      { status: 404 },
    );
  const destination = request.nextUrl.clone();
  destination.pathname = "/";
  destination.search = "";
  return NextResponse.redirect(destination);
}
export const config = {
  matcher: [
    "/api/:path*",
    "/workspace/:path*",
    "/login",
    "/setup",
    "/auth/:path*",
    "/mock/:path*",
    "/showcase/:path*",
    "/artifacts/:path*",
  ],
};
