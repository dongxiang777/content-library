/**
 * @file Channels page runtime environment override.
 */
if (typeof WXEnv === "undefined") {
  throw new Error("env.js must be loaded before env.channels.js");
}

(() => {
  // Same-origin hostname for channels MITM reverse-proxy paths.
  // channels.js prefers same-origin WSS first (wss://channels.weixin.qq.com/ws/*),
  // then optional loopback ws:// via apiServer* config. Mixed-content blocks
  // loopback ws from HTTPS pages in WeChatAppEx/Chromium.
  const pageHost =
    typeof location !== "undefined" && location.hostname
      ? location.hostname
      : "";
  const pageProtocol =
    typeof location !== "undefined" && location.protocol
      ? location.protocol.replace(":", "")
      : "https";
  const onChannelsHost =
    pageHost === "channels.weixin.qq.com" ||
    pageHost.endsWith(".channels.weixin.qq.com");

  const env = {
    channelsHostname: onChannelsHost ? pageHost : "kf.qq.com",
    channelsProtocol: onChannelsHost
      ? pageProtocol || "https"
      : "https",
    downloadHostname: "weixin110.qq.com",
    downloadProtocol: "https",
  };

  const cfg = WXEnv.config;
  if (cfg.apiServerProtocol && cfg.apiServerAddr) {
    env.assetsFallbackBase =
      WXEnv.origin(
        cfg.apiServerProtocol,
        WXEnv.normalizeHostAddr(cfg.apiServerAddr),
      ) + "/__wx_channels_assets";
  }

  WXEnv.applyRuntimeEnv(env);
})();
