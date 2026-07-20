function getShortUri(data) {
  var u = new URL(decodeURIComponent(data.url));
  var pathname = u.pathname;
  var m = pathname.match(/\/sph\/([a-zA-Z0-9]{1,})/);
  if (m) {
    return m[1];
  }
  return u.searchParams.get("id");
}
async function fetchExportIdWithShareId(data) {
  if (!data.url) {
    return [new Error("missing url"), null];
  }
  var uri = getShortUri(data);
  if (!uri) {
    return [new Error("can't get the uri from url, " + data.url), null];
  }
  await WXU.load_script(__wx_asset_url("/lib/axios.min.js"));
  await WXU.load_script(__wx_asset_url("/lib/getFeedInfo.js"));
  // await WXU.load_script(__wx_asset_url("/lib/merlin.js"));
  if (typeof getFeedInfo !== "function") {
    return [new Error("the getFeedInfo is not a function"), null];
  }
  var payload = {
    baseReq: {
      generalToken: "",
    },
    shortUri: uri,
  };
  /** @type {SharedFeedProfileResp} */
  try {
    var shared = await getFeedInfo(payload);
    if (shared.data) {
      if (shared.data.sceneInfo) {
        if (shared.data.sceneInfo.dynamicExportId) {
          return [null, shared.data.sceneInfo.dynamicExportId];
        }
        return [new Error("missing 'sceneInfo.dynamicExportId'"), null];
      }
      if (shared.data.errMsg) {
        if (shared.data.errMsg.title) {
          return [new Error(shared.data.errMsg.title), null];
        }
      }
    }
    return [new Error("getFeedInfo failed"), null];
  } catch (err) {
    return [err, null];
  }
}
async function fetchFeedProfileWith(data) {
  if (data.url) {
    if (data.url.match(/sph/)) {
      var [err, eid] = await fetchExportIdWithShareId(data);
      if (err) {
        var m = data.url.match(/\/([a-zA-Z0-9]{1,})$/);
        if (m[1]) {
          data.eid = m[1];
        } else {
          return [err, null];
        }
      } else {
        data.eid = eid;
      }
    } else {
      var u = new URL(decodeURIComponent(data.url));
      data.oid = WXU.API.decodeBase64ToUint64String(u.searchParams.get("oid"));
      data.nid = WXU.API.decodeBase64ToUint64String(u.searchParams.get("nid"));
    }
  }
  let payload = {
    needObject: 1,
    lastBuffer: "",
    scene: data.eid ? 141 : 146,
    direction: 2,
    identityScene: 2,
    pullScene: 6,
    objectid: (() => {
      if (data.eid) {
        return undefined;
      }
      if (data.oid.includes("_")) {
        return data.oid.split("_")[0];
      }
      return data.oid;
    })(),
    objectNonceId: data.eid ? undefined : data.nid,
    encrypted_objectid: data.eid || "",
  };
  if (data.eid) {
    payload.traceBuffer = undefined;
  }
  try {
    var r = await WXU.API.finderGetCommentDetail(payload);
    return [null, r, payload];
  } catch (err) {
    return [err, null, null];
  }
}

function ChannelsWebsocketClient() {
  let reconnect_timer = null;
  let reconnect_attempt = 0;
  let active_ws = null;
  // Rotate across candidate URLs when a path fails (proxy hang / mixed
  // content / 1006). Sticky on the last URL that opened successfully.
  let url_rotate = 0;
  const CONNECTING_HANG_MS = 5000;

  function tip(msg) {
    console.log("[DOWNLOADER]", msg);
    if (typeof WXU !== "undefined" && typeof WXU.tip === "function") {
      WXU.tip({ msg: String(msg) });
    }
  }

  function errTip(msg) {
    console.error("[DOWNLOADER]", msg);
    if (typeof WXU !== "undefined" && typeof WXU.error === "function") {
      // alert:0 — reconnect storms must not spam modal dialogs.
      WXU.error({ msg: String(msg), alert: 0 });
    }
  }

  function candidateURLs() {
    if (
      typeof WXEnv !== "undefined" &&
      typeof WXEnv.channelsWSCandidates === "function"
    ) {
      return WXEnv.channelsWSCandidates();
    }
    if (typeof WXEnv !== "undefined" && WXEnv.channelsWSURL) {
      return [WXEnv.channelsWSURL];
    }
    return [];
  }

  function scheduleReconnect() {
    if (reconnect_timer) {
      clearTimeout(reconnect_timer);
      reconnect_timer = null;
    }
    const delay = Math.min(15000, 1000 * Math.pow(2, reconnect_attempt));
    reconnect_attempt += 1;
    reconnect_timer = setTimeout(() => {
      methods.connect_local_ws();
    }, delay);
  }

  const methods = {
    connect_local_ws() {
      if (reconnect_timer) {
        clearTimeout(reconnect_timer);
        reconnect_timer = null;
      }
      if (
        active_ws &&
        (active_ws.readyState === WebSocket.OPEN ||
          active_ws.readyState === WebSocket.CONNECTING)
      ) {
        return active_ws;
      }

      const urls = candidateURLs();
      if (!urls.length) {
        errTip("channels ws: no candidate URLs");
        scheduleReconnect();
        return null;
      }

      const idx = url_rotate % urls.length;
      const ws_url = urls[idx];
      tip(
        "channels ws connecting [" +
          (idx + 1) +
          "/" +
          urls.length +
          "]: " +
          ws_url,
      );

      let ws;
      try {
        ws = new WebSocket(ws_url);
      } catch (err) {
        errTip(
          "channels ws constructor failed: " +
            ws_url +
            " — " +
            (err && err.message ? err.message : err),
        );
        url_rotate += 1;
        scheduleReconnect();
        return null;
      }

      active_ws = ws;
      let opened = false;

      // WeChatAppEx sometimes leaves WSS stuck in CONNECTING without firing
      // onerror/onclose (no CONNECT reaches the MITM). Force-rotate.
      const hang_timer = setTimeout(() => {
        if (ws.readyState === WebSocket.CONNECTING) {
          errTip("channels ws CONNECTING hang, rotating: " + ws_url);
          url_rotate += 1;
          try {
            ws.close();
          } catch (_e) {
            /* ignore */
          }
        }
      }, CONNECTING_HANG_MS);

      ws.onopen = () => {
        opened = true;
        clearTimeout(hang_timer);
        reconnect_attempt = 0;
        // Keep url_rotate pointing at the working candidate.
        url_rotate = idx;
        tip("channels ws 已连接: " + ws_url);
      };
      ws.onclose = (e) => {
        clearTimeout(hang_timer);
        if (active_ws === ws) {
          active_ws = null;
        }
        if (!opened) {
          // Failed before open — try next candidate on the next attempt.
          url_rotate += 1;
        }
        errTip(
          "channels ws closed url=" +
            ws_url +
            " code=" +
            e.code +
            " reason=" +
            (e.reason || ""),
        );
        // Auto-reconnect so restarts / transient proxy gaps recover without
        // forcing the user to fully re-open the channels page.
        scheduleReconnect();
      };
      ws.onerror = () => {
        // onclose follows; keep a lightweight diagnostic with the URL.
        errTip("channels ws error url=" + ws_url);
      };
      ws.onmessage = (ev) => {
        const [err, msg] = WXU.parseJSON(ev.data);
        if (err) {
          return;
        }
        if (msg.type === "api_call") {
          methods.__wx_handle_api_call(msg.data, ws);
        }
      };
      return ws;
    },
    async __wx_handle_api_call(msg, socket) {
      var { id, key, data } = msg;
      console.log("[DOWNLOADER]__wx_handle_api_call", id, key, data);
      function resp(body) {
        socket.send(
          JSON.stringify({
            id,
            data: body,
          }),
        );
      }
      if (key === "key:channels:contact_list") {
        let payload = {
          query: data.keyword,
          scene: 13,
          lastBuff: data.next_marker
            ? decodeURIComponent(data.next_marker)
            : "",
          requestId: String(new Date().valueOf()),
        };
        var r = await WXU.API2.finderSearch(payload);
        console.log("[DOWNLOADER]finderSearch", r, payload);
        /** @type {SearchResp} */
        var { infoList, objectList } = r.data;
        resp({
          ...r,
          payload,
        });
        return;
      }
      if (key === "key:channels:video_search") {
        // PC 视频号前端 search store 场景值（见 FinderSearch/search.publish）：
        // 13=Live, 19=PC_Main_Page(账号+动态), 20=Within_UserPage,
        // 21=PC_Account, 22=PC_MegaVideo, 23=PC_Live
        // 动态/视频列表必须用 19，0~15 会返回空 objectList。
        //
        // 筛选说明（研究自 PC 搜一搜 wepkg + TikHub 对齐）：
        // - 搜一搜走 native getSearchData(type=14 VIDEO)，筛选用 filterExtReqParams
        //   {key,textValue/uintValue}；PC channels finderSearch 常忽略这些字段。
        // - sort: 0综合 / 1最新 / 2最热
        // - time_range: 接受 搜一搜枚举 0/1/2/3（不限/一天/七天/半年）或天数 0/1/7/180
        // - scope: 0不限 / 1已关注 / 2最近看过 / 3朋友赞过（PC 端后端大多无效）
        // 因此：先透传后端；再对 objectList 做本地时间过滤与排序，保证筛选可见生效。
        var scene =
          data.scene !== undefined && data.scene !== null && data.scene !== ""
            ? Number(data.scene)
            : 19;
        if (!Number.isFinite(scene)) scene = 19;

        /** @returns {{enum:number, seconds:number}} */
        function normalizeTimeRange(raw) {
          if (raw === undefined || raw === null || raw === "") {
            return { enum: 0, seconds: 0 };
          }
          var n = Number(raw);
          if (!Number.isFinite(n) || n <= 0) return { enum: 0, seconds: 0 };
          // 搜一搜 publish_time 枚举：0不限 1一天 2七天 3半年
          if (n === 1) return { enum: 1, seconds: 86400 };
          if (n === 2) return { enum: 2, seconds: 7 * 86400 };
          if (n === 3) return { enum: 3, seconds: 180 * 86400 };
          // 兼容天数写法：7 / 180 / 365 …
          if (n === 7) return { enum: 2, seconds: 7 * 86400 };
          if (n === 180) return { enum: 3, seconds: 180 * 86400 };
          if (n > 3) return { enum: 0, seconds: Math.floor(n * 86400) };
          return { enum: 0, seconds: 0 };
        }

        function engagementScore(obj) {
          if (!obj || typeof obj !== "object") return 0;
          var keys = [
            "likeCount",
            "favCount",
            "forwardCount",
            "readCount",
            "commentCount",
          ];
          var sum = 0;
          for (var i = 0; i < keys.length; i++) {
            var v = Number(obj[keys[i]]);
            if (Number.isFinite(v)) sum += v;
          }
          // 部分字段在 contact / objectExtend / monotonicData 里
          var contact = obj.contact || {};
          var ext = obj.objectExtend || obj.object_extend || {};
          var mono = obj.monotonicData || ext.monotonicData || {};
          var countInfo =
            (mono && mono.countInfo) ||
            (mono && mono.countinfo) ||
            mono.Countinfo ||
            {};
          ["likeCount", "favCount", "forwardCount", "likecount", "favcount"].forEach(
            function (k) {
              var vv = Number(countInfo[k] || contact[k] || ext[k]);
              if (Number.isFinite(vv)) sum += vv;
            },
          );
          return sum;
        }

        function createTimeOf(obj) {
          var t = Number(
            (obj && (obj.createtime || obj.createTime || obj.create_time)) || 0,
          );
          return Number.isFinite(t) ? t : 0;
        }

        /**
         * 对 finderSearch 返回的 objectList 做本地筛选/排序。
         * PC 后端常忽略 sortType/timeRange，本地处理后保证接口语义成立。
         */
        function applyLocalVideoFilters(list, sort, timeSeconds) {
          var out = Array.isArray(list) ? list.slice() : [];
          if (timeSeconds > 0) {
            var cutoff = Math.floor(Date.now() / 1000) - timeSeconds;
            out = out.filter(function (o) {
              var ct = createTimeOf(o);
              // createtime 缺失时保留，避免误删
              return !ct || ct >= cutoff;
            });
          }
          if (sort === 1) {
            out.sort(function (a, b) {
              return createTimeOf(b) - createTimeOf(a);
            });
          } else if (sort === 2) {
            out.sort(function (a, b) {
              var d = engagementScore(b) - engagementScore(a);
              if (d !== 0) return d;
              return createTimeOf(b) - createTimeOf(a);
            });
          }
          return out;
        }

        var payload = {
          query: data.keyword,
          scene: scene,
          requestId: data.request_id
            ? String(data.request_id)
            : String(Date.now()),
        };
        // 分页：与官方 searchMoreFeeds 一致，同时传 offset + lastBuff
        if (data.next_marker) {
          payload.lastBuff = decodeURIComponent(data.next_marker);
        } else if (data.last_buff) {
          payload.lastBuff = data.last_buff;
        }
        if (data.offset !== undefined && data.offset !== null && data.offset !== "") {
          var off = Number(data.offset);
          if (Number.isFinite(off)) payload.offset = off;
        }

        var sortVal = null;
        if (data.sort !== undefined && data.sort !== null && data.sort !== "") {
          sortVal = Number(data.sort);
          if (Number.isFinite(sortVal)) {
            // 主字段（视频号/搜一搜常见命名）
            payload.sortType = sortVal;
            // 兼容部分网关/历史字段
            payload.sort_type = sortVal;
            payload.docSortType = sortVal;
          } else {
            sortVal = null;
          }
        }

        var timeNorm = normalizeTimeRange(data.time_range);
        if (timeNorm.enum > 0 || timeNorm.seconds > 0) {
          // 枚举 0/1/2/3（与搜一搜 publish_time 对齐）
          payload.timeRange = timeNorm.enum > 0 ? timeNorm.enum : 0;
          payload.time_range = payload.timeRange;
          // 部分实现用秒/天
          if (timeNorm.seconds > 0) {
            payload.timeCondition = timeNorm.seconds;
          }
        }

        var scopeVal = null;
        if (data.scope !== undefined && data.scope !== null && data.scope !== "") {
          scopeVal = Number(data.scope);
          if (Number.isFinite(scopeVal) && scopeVal > 0) {
            // 范围：0不限 1已关注 2最近看过 3朋友赞过
            // PC finderSearch 通常无效；仍透传便于后端实验/抓包对照
            payload.filterType = scopeVal;
            payload.scope = scopeVal;
            payload.finderTabInteractionType = String(scopeVal);
          } else {
            scopeVal = null;
          }
        }

        if (data.finder_username) {
          payload.finderUsername = data.finder_username;
        }

        // 搜一搜式 extReqParams（native getSearchData 路径用；finderSearch 可能忽略）
        var extReqParams = [];
        if (sortVal !== null && sortVal > 0) {
          extReqParams.push({
            key: "sortType",
            uintValue: sortVal,
            textValue: String(sortVal),
          });
        }
        if (timeNorm.enum > 0) {
          extReqParams.push({
            key: "timeRange",
            textValue: JSON.stringify([String(timeNorm.enum)]),
          });
          extReqParams.push({
            key: "docPubTime",
            textValue: JSON.stringify([String(timeNorm.enum)]),
          });
        }
        if (scopeVal !== null && scopeVal > 0) {
          extReqParams.push({
            key: "HomePageAdvanceSearchScope",
            textValue: JSON.stringify([String(scopeVal)]),
          });
          extReqParams.push({
            key: "finderTabInteractionType",
            textValue: JSON.stringify([String(scopeVal)]),
          });
        }
        if (extReqParams.length) {
          payload.extReqParams = extReqParams;
          payload.filterExtReqParams = extReqParams;
        }

        if (!WXU.API2 || typeof WXU.API2.finderSearch !== "function") {
          resp({
            errCode: 1011,
            errMsg: "finderSearch unavailable (open channels page first)",
          });
          return;
        }

        // 必须通过实例方法调用以保留 this（class method 内使用 this.post）。
        // 不可写成 const fn = WXU.API2.finderSearch; fn(payload) —— 会丢失 this 导致
        // "Cannot read properties of undefined (reading 'post')"。
        console.log(
          "[DOWNLOADER]video_search finderSearch payload=",
          JSON.stringify(payload),
        );
        try {
          var r = await WXU.API2.finderSearch(payload);
          var rawList =
            (r && r.data && r.data.objectList) ||
            (r && r.data && r.data.object_list) ||
            [];
          var beforeCount = Array.isArray(rawList) ? rawList.length : 0;
          var filtered = applyLocalVideoFilters(
            rawList,
            sortVal,
            timeNorm.seconds,
          );
          if (r && r.data) {
            r.data.objectList = filtered;
            // 标记本地筛选已应用，便于排查
            r.data._localFilter = {
              sort: sortVal,
              time_range_enum: timeNorm.enum,
              time_seconds: timeNorm.seconds,
              scope: scopeVal,
              before: beforeCount,
              after: filtered.length,
            };
          }
          console.log(
            "[DOWNLOADER]video_search result",
            "objectList=",
            filtered.length,
            "raw=",
            beforeCount,
            "infoList=",
            (r && r.data && r.data.infoList && r.data.infoList.length) || 0,
            "continue=",
            r && r.data && r.data.objectContinueFlag,
            "offset=",
            r && r.data && r.data.offset,
            "filter=",
            r && r.data && r.data._localFilter,
          );
          resp({
            ...r,
            payload,
          });
        } catch (err) {
          console.log("[DOWNLOADER]video_search error:", err && err.message);
          resp({
            errCode: 1011,
            errMsg: (err && err.message) || String(err),
            payload,
          });
        }
        return;
      }
      if (key === "key:channels:feed_list") {
        let payload = {
          username: data.username,
          finderUsername: __wx_username,
          lastBuffer: data.next_marker
            ? decodeURIComponent(data.next_marker)
            : "",
          needFansCount: 0,
          objectId: "0",
        };
        let r = await WXU.API.finderUserPage(payload);
        console.log("[DOWNLOADER]finderUserPage", r);
        /** @type {ChannelsObject[]} */
        const object = r.data.object || [];
        resp({
          ...r,
          payload,
        });
        return;
      }
      if (key === "key:channels:live_replay_list") {
        let payload = {
          username: data.username,
          finderUsername: __wx_username || data.username,
          lastBuffer: data.next_marker
            ? decodeURIComponent(data.next_marker)
            : "",
          needFansCount: 0,
          objectId: "0",
        };
        var r = await WXU.API3.finderLiveUserPage(payload);
        console.log("[DOWNLOADER]finderLiveUserPage", r);
        resp({
          ...r,
          payload,
        });
        return;
      }
      if (key === "key:channels:interactioned_list") {
        let payload = {
          lastBuffer: data.next_marker
            ? decodeURIComponent(data.next_marker)
            : "",
          tabFlag: data.flag ? Number(data.flag) : 7,
        };
        var r = await WXU.API4.finderGetInteractionedFeedList(payload);
        console.log("[DOWNLOADER]finderGetInteractionedFeedList", r);
        resp({
          ...r,
          payload,
        });
        return;
      }
      if (key === "key:channels:follow_list") {
        let payload = {
          finderUsername: __wx_username,
          lastBuffer: data.next_marker
            ? decodeURIComponent(data.next_marker)
            : "",
        };
        try {
          var r = await WXU.API4.finderGetFollowList(payload);
          console.log("[DOWNLOADER]finderGetFollowList", r, payload);
          resp({
            ...r,
            payload,
          });
        } catch (err) {
          resp({
            errCode: 1011,
            errMsg: err.message,
            payload,
          });
        }
        return;
      }
      if (key === "key:channels:feed_profile") {
        console.log("before finderGetCommentProfile", data);
        var [err, r, payload] = await fetchFeedProfileWith(data);
        if (err) {
          resp({
            errCode: 1011,
            errMsg: err.message,
            payload: null,
          });
          return;
        }
        /** @type {MediaProfileResp} */
        var { object } = r.data;
        resp({
          ...r,
          payload,
        });
        return;
      }
      if (key === "key:channels:fetch_feed_comment_list") {
        // console.log("[DOWNLOADER]key:channels:fetch_feed_comment_list");
        if (!data.oid) {
          resp({
            errCode: 1011,
            errMsg: "missing oid",
            payload: null,
          });
          return;
        }
        if (!data.nid && !data.comment_id) {
          resp({
            errCode: 1011,
            errMsg: "missing nid or comment_id",
            payload: null,
          });
          return;
        }
        try {
          var payload = data.comment_id
            ? {
                direction: 2,
                identityScene: 2,
                objectId: data.oid,
                lastBuffer:
                  data.next_marker === "" ? undefined : data.next_marker,
                rootCommentId: data.comment_id,
              }
            : {
                finderBasereq: {
                  scene: 140,
                  ctxInfo: {
                    clientReportBuff: '{"entranceId":"1002"}',
                  },
                  objectBaseInfos: [],
                },
                objectId: data.oid,
                direction: 2,
                objectNonceId: data.nid,
                identityScene: 2,
                lastBuffer:
                  data.next_marker === "" ? undefined : data.next_marker,
                enterSessionId: String(Date.now()),
              };
          var r = await WXU.API.finderGetCommentList(payload);
          resp({
            ...r,
            payload,
          });
        } catch (err) {
          resp({
            errCode: 1011,
            errMsg: err.message,
            payload: null,
          });
        }
        return;
      }
      if (key === "key:channels:feed_share_url") {
        // console.log("[DOWNLOADER]fetchFeedShareUrl");
        if (!data.oid) {
          resp({
            errCode: 1011,
            errMsg: "missing oid",
            payload: null,
          });
          return;
        }
        var payload = {
          objectId: data.oid,
        };
        try {
          var r = await WXU.API.finderGetFeedH5Url(payload);
          resp({
            ...r,
            payload,
          });
        } catch (err) {
          resp({
            errCode: 1011,
            errMsg: err.message,
            payload,
          });
        }
        return;
      }
      if (key === "key:channels:reload") {
        console.log("[DOWNLOADER]reloading page");
        resp({
          msg: "reloading",
        });
        setTimeout(() => {
          window.location.reload();
        }, 500);
        return;
      }
      resp({
        errCode: 1000,
        errMsg: "未匹配的key",
        payload: msg,
      });
      return;
    },
  };
  return {
    methods,
  };
}

var ws_client$ = ChannelsWebsocketClient();
// Defer the first connect past head-script parse / parallel asset CONNECTs.
// Observed failure mode: same-origin WSS opens fine once the page is idle
// (reconnect), but a connect during the initial multi-CONNECT storm never
// reaches the MITM and never fires onerror/onclose.
(function scheduleChannelsWSConnect() {
  function start() {
    try {
      ws_client$.methods.connect_local_ws();
    } catch (e) {
      console.error("[DOWNLOADER]channels ws schedule failed", e);
      if (typeof WXU !== "undefined" && typeof WXU.error === "function") {
        WXU.error({
          msg:
            "channels ws schedule failed: " +
            (e && e.message ? e.message : e),
          alert: 0,
        });
      }
    }
  }
  var delayMs = 250;
  if (typeof document !== "undefined" && document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      setTimeout(start, delayMs);
    });
  } else {
    setTimeout(start, delayMs);
  }
})();
