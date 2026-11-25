export default {
  async fetch() {
    return new Response("Archive branch preview disabled.\n", {
      status: 404,
      headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" },
    });
  },
};
