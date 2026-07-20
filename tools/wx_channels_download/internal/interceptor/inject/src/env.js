/**
 * @file Injected runtime environment, service addresses, and global config entry.
 */
if (typeof window.__wx_channels_config__ === "undefined") {
  window.__wx_channels_config__ = {};
}
if (typeof window.WXVariable === "undefined") {
  window.WXVariable = {};
}
if (typeof window.__wx_channels_env__ === "undefined") {
  window.__wx_channels_env__ = {};
}

var WXEnv = (() => {
  const defaults = {
    channelsProtocol: "https",
    channelsHostname: "kf.qq.com",
    downloadProtocol: "https",
    downloadHostname: "weixin110.qq.com",
    assetsFallbackBase: "http://127.0.0.1:2022/__wx_channels_assets",
    MaxRunning: 5,
  };
  const runtimeEnv = window.__wx_channels_env__;
  const ua = navigator.userAgent || navigator.platform || "";

  function config() {
    return {
      ...(window.__wx_channels_config__ || {}),
      ...(window.WXVariable || {}),
    };
  }

  function ownValue(source, name) {
    if (source && Object.prototype.hasOwnProperty.call(source, name)) {
      return source[name];
    }
    return undefined;
  }

  function explicitEnvValue(name) {
    const runtimeValue = ownValue(runtimeEnv, name);
    if (typeof runtimeValue !== "undefined") {
      return runtimeValue;
    }
    return ownValue(config(), name);
  }

  function envValue(name) {
    const value = explicitEnvValue(name);
    if (typeof value !== "undefined") {
      return value;
    }
    return defaults[name];
  }

  function applyRuntimeEnv(values) {
    if (!values || typeof values !== "object") {
      return runtimeEnv;
    }
    Object.assign(runtimeEnv, values);
    refreshLegacyGlobals();
    return runtimeEnv;
  }

  function hostPort(hostname, port) {
    const host = normalizeHostname(hostname);
    if (!host) {
      return "";
    }
    if (
      port === undefined ||
      port === null ||
      port === "" ||
      Number(port) === 0
    ) {
      return host;
    }
    return host + ":" + port;
  }

  function normalizeHostname(hostname) {
    const value = String(hostname || "").trim();
    if (!value) {
      return "";
    }
    const unwrapped =
      value.startsWith("[") && value.endsWith("]") ? value.slice(1, -1) : value;
    if (unwrapped === "0.0.0.0" || unwrapped === "::") {
      return "127.0.0.1";
    }
    return value;
  }

  function normalizeHostAddr(addr) {
    const value = String(addr || "").trim();
    if (!value) {
      return "";
    }
    const match = value.match(/^(\[[^\]]+\]|[^:]+)(?::(\d+))?$/);
    if (!match) {
      return value;
    }
    const host = normalizeHostname(match[1]);
    return match[2] ? host + ":" + match[2] : host;
  }

  function origin(protocol, addr) {
    if (!protocol || !addr) {
      return "";
    }
    return protocol + "://" + addr;
  }

  function wsProtocol(protocol) {
    return protocol === "https" ? "wss" : "ws";
  }

  function channelsServer() {
    return {
      addr: envValue("channelsHostname") || defaults.channelsHostname,
      protocol: envValue("channelsProtocol") || defaults.channelsProtocol,
    };
  }

  function downloadServer() {
    return {
      addr: envValue("downloadHostname") || defaults.downloadHostname,
      protocol: envValue("downloadProtocol") || defaults.downloadProtocol,
    };
  }

  function assetsBaseURL() {
    const cfg = window.__wx_channels_config__ || {};
    const explicitBase = explicitEnvValue("assetsBaseURL");
    if (explicitBase) {
      return String(explicitBase).replace(/\/$/, "");
    }
    if (cfg.apiServerProtocol && cfg.apiServerAddr) {
      return (
        origin(cfg.apiServerProtocol, cfg.apiServerAddr) +
        "/__wx_channels_assets"
      );
    }
    if (cfg.Protocol && cfg.Addr) {
      return origin(cfg.Protocol, cfg.Addr) + "/__wx_channels_assets";
    }
    return envValue("assetsFallbackBase");
  }

  function assetUrl(path) {
    const base = assetsBaseURL();
    if (path.startsWith("/lib/")) {
      const version = encodeURIComponent(
        window.__wx_channels_version__ || "static",
      );
      return `${base}${path}?v=${version}`;
    }
    return `${base}${path}`;
  }

  function legacyGlobals() {
    const channels = channelsServer();
    const download = downloadServer();
    return {
      ChannelsHostname: channels.addr,
      DownloadHostname: download.addr,
      APIServerProtocol: download.protocol,
      WSServerProtocol: wsProtocol(download.protocol),
      WXUserAgent: ua,
      isWin: /Windows|Win/i.test(ua),
      isWeChatBrowser: /MicroMessenger/i.test(ua),
      __wx_assets_base: assetsBaseURL(),
    };
  }

  function refreshLegacyGlobals() {
    const values = legacyGlobals();
    Object.keys(values).forEach((name) => {
      window[name] = values[name];
    });
    return values;
  }

  return {
    defaults,
    runtimeEnv,
    applyRuntimeEnv,
    refreshLegacyGlobals,
    get config() {
      return config();
    },
    get userAgent() {
      return ua;
    },
    get isWin() {
      return /Windows|Win/i.test(ua);
    },
    get isWeChatBrowser() {
      return /MicroMessenger/i.test(ua);
    },
    get isChannels() {
      return window.location.href.includes("weixin.qq.com");
    },
    get isWxwork() {
      return window.ua && window.ua.includes("wxwork");
    },
    hostPort,
    normalizeHostname,
    normalizeHostAddr,
    origin,
    wsProtocol,
    get configuredAPI() {
      return downloadServer();
    },
    get localAPI() {
      return channelsServer();
    },
    get remoteAPI() {
      return downloadServer();
    },
    get api() {
      return downloadServer();
    },
    get apiServerAddr() {
      return downloadServer().addr;
    },
    get apiServerProtocol() {
      return downloadServer().protocol;
    },
    get apiOrigin() {
      const api = downloadServer();
      return origin(api.protocol, api.addr);
    },
    get wsServerProtocol() {
      return wsProtocol(downloadServer().protocol);
    },
    get remoteAPIOrigin() {
      return this.downloadOrigin;
    },
    get localAPIOrigin() {
      return this.channelsOrigin;
    },
    get officialAccountOrigin() {
      return this.downloadOrigin;
    },
    get assetsBaseURL() {
      return assetsBaseURL();
    },
    assetUrl,
    get channelsHostname() {
      return channelsServer().addr;
    },
    get downloadHostname() {
      return downloadServer().addr;
    },
    get channelsOrigin() {
      const channels = channelsServer();
      return origin(channels.protocol, channels.addr);
    },
    get downloadOrigin() {
      const download = downloadServer();
      return origin(download.protocol, download.addr);
    },
    get channelsWSURL() {
      const channels = channelsServer();
      return (
        wsProtocol(channels.protocol) +
        "://" +
        channels.addr +
        "/ws/channels"
      );
    },
    /**
     * Ordered WebSocket URL candidates for channels control plane.
     *
     * Prefer same-origin WSS (MITM reverse-proxy on channels.weixin.qq.com)
     * first: Chromium/WeChatAppEx blocks mixed-content `ws://` from HTTPS
     * pages, so a loopback `ws://127.0.0.1` candidate often never completes
     * a real handshake (or only produces a plain GET probe → handler 400).
     * Loopback remains second for environments that allow it; kf.qq.com is
     * the legacy reverse-proxy hop.
     */
    channelsWSCandidates() {
      const list = [];
      const cfg = config();
      const channels = channelsServer();
      if (channels.addr) {
        list.push(
          wsProtocol(channels.protocol) +
            "://" +
            channels.addr +
            "/ws/channels",
        );
      }
      if (cfg.apiServerProtocol && cfg.apiServerAddr) {
        const addr = normalizeHostAddr(cfg.apiServerAddr);
        if (addr) {
          list.push(
            wsProtocol(cfg.apiServerProtocol) + "://" + addr + "/ws/channels",
          );
        }
      }
      list.push("wss://kf.qq.com/ws/channels");
      const seen = Object.create(null);
      return list.filter((url) => {
        if (!url || seen[url]) return false;
        seen[url] = true;
        return true;
      });
    },
    get downloaderWSURL() {
      return this.wsServerProtocol + "://" + this.apiServerAddr + "/ws/downloader";
    },
    /**
     * Ordered WebSocket URL candidates for the downloader panel.
     * Same rationale as channelsWSCandidates: try loopback first.
     */
    downloaderWSCandidates() {
      const list = [];
      const cfg = config();
      if (cfg.apiServerProtocol && cfg.apiServerAddr) {
        const addr = normalizeHostAddr(cfg.apiServerAddr);
        if (addr) {
          list.push(
            wsProtocol(cfg.apiServerProtocol) +
              "://" +
              addr +
              "/ws/downloader",
          );
        }
      }
      const download = downloadServer();
      if (download.addr) {
        list.push(
          wsProtocol(download.protocol) +
            "://" +
            download.addr +
            "/ws/downloader",
        );
      }
      const seen = Object.create(null);
      return list.filter((url) => {
        if (!url || seen[url]) return false;
        seen[url] = true;
        return true;
      });
    },
    get mpWSURL() {
      return this.wsServerProtocol + "://" + this.apiServerAddr + "/ws/mp";
    },
  };
})();

WXEnv.refreshLegacyGlobals();
var ChannelsHostname = window.ChannelsHostname;
var DownloadHostname = window.DownloadHostname;
var APIServerProtocol = window.APIServerProtocol;
var WSServerProtocol = window.WSServerProtocol;
var WXUserAgent = window.WXUserAgent;
var isWin = window.isWin;
var isWeChatBrowser = window.isWeChatBrowser;
var __wx_assets_base = window.__wx_assets_base;
function __wx_asset_url(path) {
  return WXEnv.assetUrl(path);
}
