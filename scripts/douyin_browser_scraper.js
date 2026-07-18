/**
 * 抖音浏览器端采集脚本
 * 使用方式：在浏览器中注入此脚本，通过 window.__douyinScraper 调用
 *
 * 优势：
 * - 无需处理 cookie 提取、HttpOnly 限制
 * - 无需 a_bogus 签名生成（浏览器自动处理）
 * - 浏览器指纹完全一致，不会被反爬检测
 * - 登录状态自动保持
 */

window.__douyinScraper = {

  /**
   * 关键词搜索采集
   * @param {string} keyword - 搜索关键词
   * @param {number} num - 采集数量
   * @param {string} sortType - 排序: '0'=综合排序, '1'=最多点赞, '2'=最新发布
   * @param {string} publishTime - 发布时间: '0'=不限, '1'=一天内, '7'=一周内, '180'=半年内
   * @returns {Array} 采集结果
   */
  async search(keyword, num = 20, sortType = '1', publishTime = '0') {
    const results = [];
    let offset = 0;
    const batchSize = 10;

    while (results.length < num) {
      const params = new URLSearchParams({
        device_platform: 'webapp',
        aid: '6383',
        channel: 'channel_pc_web',
        search_channel: 'aweme_general',
        enable_history: '1',
        keyword: keyword,
        search_source: 'tab_search',
        query_correct_type: '1',
        is_filter_search: '1',
        from_group_id: '',
        offset: String(offset),
        count: String(batchSize),
        need_filter_settings: offset === 0 ? '1' : '0',
        filter_selected: JSON.stringify({
          sort_type: sortType,
          publish_time: publishTime,
          filter_duration: '',
          search_range: '0',
          content_type: '0'
        }),
        list_type: 'single',
        update_version_code: '170400',
        pc_client_type: '1',
        version_code: '190600',
        version_name: '19.6.0',
        cookie_enabled: 'true',
        screen_width: '2560',
        screen_height: '1440',
        browser_language: 'zh-CN',
        browser_platform: 'MacIntel',
        browser_name: 'Chrome',
        browser_version: '150.0.0.0',
        browser_online: 'true',
        engine_name: 'Blink',
        engine_version: '150.0.0.0',
        os_name: 'Mac+OS',
        os_version: '10.15.7',
        device_memory: '8',
        platform: 'PC',
        downlink: '10',
        effective_type: '4g',
        round_trip_time: '50',
        webid: '7527614565380851243'
      });

      try {
        const resp = await fetch(
          'https://www.douyin.com/aweme/v1/web/general/search/single/?' + params.toString()
        );
        const data = await resp.json();

        if (data.status_code !== 0 || !data.data) {
          console.error('API error:', data.status_code, data.status_msg);
          break;
        }

        for (const item of data.data) {
          const info = item.aweme_info || item;
          const stats = info.statistics || {};
          results.push({
            desc: info.desc || '',
            author_nickname: info.author?.nickname || '',
            author_sec_uid: info.author?.sec_uid || '',
            author_uid: info.author?.uid || '',
            aweme_id: info.aweme_id || '',
            digg_count: stats.digg_count || 0,
            comment_count: stats.comment_count || 0,
            share_count: stats.share_count || 0,
            collect_count: stats.collect_count || 0,
            create_time: info.create_time || 0,
            duration: info.duration || 0,
            video_url: info.video?.play_addr?.url_list?.[0] || '',
            source_keyword: keyword
          });
        }

        if (!data.has_more) break;
        offset += data.data.length;

        // 随机延迟 1.5~3 秒，模拟人类操作
        await new Promise(r => setTimeout(r, 1500 + Math.random() * 1500));
      } catch (e) {
        console.error('Fetch error:', e);
        break;
      }
    }

    return results.slice(0, num);
  },

  /**
   * 用户主页采集
   * @param {string} secUid - 用户的 sec_uid
   * @param {number} num - 采集数量
   * @returns {Array} 用户作品列表
   */
  async userProfile(secUid, num = 20) {
    const results = [];
    let maxCursor = 0;
    const batchSize = 18;

    while (results.length < num) {
      const params = new URLSearchParams({
        device_platform: 'webapp',
        aid: '6383',
        channel: 'channel_pc_web',
        sec_user_id: secUid,
        max_cursor: String(maxCursor),
        count: String(batchSize),
        update_version_code: '170400',
        pc_client_type: '1',
        version_code: '190600',
        version_name: '19.6.0',
        cookie_enabled: 'true',
        screen_width: '2560',
        screen_height: '1440',
        browser_language: 'zh-CN',
        browser_platform: 'MacIntel',
        browser_name: 'Chrome',
        browser_version: '150.0.0.0',
        browser_online: 'true',
        engine_name: 'Blink',
        engine_version: '150.0.0.0',
        os_name: 'Mac+OS',
        os_version: '10.15.7',
        device_memory: '8',
        platform: 'PC',
        downlink: '10',
        effective_type: '4g',
        round_trip_time: '50'
      });

      try {
        const resp = await fetch(
          'https://www.douyin.com/aweme/v1/web/aweme/post/?' + params.toString()
        );
        const data = await resp.json();

        if (data.status_code !== 0 || !data.aweme_list) {
          console.error('API error:', data.status_code, data.status_msg);
          break;
        }

        for (const info of data.aweme_list) {
          const stats = info.statistics || {};
          results.push({
            desc: info.desc || '',
            author_nickname: info.author?.nickname || '',
            author_sec_uid: info.author?.sec_uid || '',
            aweme_id: info.aweme_id || '',
            digg_count: stats.digg_count || 0,
            comment_count: stats.comment_count || 0,
            share_count: stats.share_count || 0,
            collect_count: stats.collect_count || 0,
            create_time: info.create_time || 0,
            duration: info.duration || 0
          });
        }

        if (!data.has_more) break;
        maxCursor = data.max_cursor;

        await new Promise(r => setTimeout(r, 1500 + Math.random() * 1500));
      } catch (e) {
        console.error('Fetch error:', e);
        break;
      }
    }

    return results.slice(0, num);
  },

  /**
   * 批量关键词搜索
   * @param {Array<string>} keywords - 关键词列表
   * @param {number} numPerKeyword - 每个关键词采集数量
   * @param {string} sortType - 排序方式
   * @returns {Array} 所有结果
   */
  async batchSearch(keywords, numPerKeyword = 10, sortType = '1') {
    const allResults = [];
    for (const keyword of keywords) {
      console.log(`Searching: ${keyword} (${numPerKeyword} results)...`);
      const results = await this.search(keyword, numPerKeyword, sortType);
      allResults.push(...results);
      console.log(`  Got ${results.length} results`);
      // 关键词间延迟
      await new Promise(r => setTimeout(r, 3000 + Math.random() * 2000));
    }
    return allResults;
  }
};

'Scraper v2 ready. Methods: search(keyword, num, sortType, publishTime), userProfile(secUid, num), batchSearch(keywords, numPerKeyword, sortType)';
