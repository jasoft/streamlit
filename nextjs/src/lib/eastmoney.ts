import axios from 'axios';
import { exec } from 'child_process';
import util from 'util';

const execPromise = util.promisify(exec);

function parseMoney(val: any): number {
  if (typeof val === 'number') {
    let num = val;
    if (Math.abs(num) <= 50000) {
      num = num * 100000000;
    }
    return num;
  }
  if (!val) return 0;
  const str = String(val).trim();
  let num = parseFloat(str.replace(/[^\d.-]/g, ''));
  if (isNaN(num)) return 0;
  if (str.includes('亿')) {
    num = num * 100000000;
  } else if (str.includes('万')) {
    num = num * 1000; // 有时也可能写为几万万，默认按国内万分缩放
  } else if (Math.abs(num) <= 50000) {
    num = num * 100000000;
  }
  return num;
}

const EM_HEADERS = {
  "Connection": "keep-alive",
  "Accept": "application/json, text/javascript, */*; q=0.01",
  "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
  "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
  "Referer": "https://data.eastmoney.com/bkzj/hy.html",
};

const SINA_HEADERS = {
  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
  "Referer": "http://vip.stock.finance.sina.com.cn/",
};

const dbCodeToName: Record<string, string> = {};
const sectorNameToSinaCode: Record<string, string> = {};

export async function fetchSnapshot(sectorType = 'industry', indicator = 'today') {
  try {
    const fsMap: any = { industry: "m:90+t:2", concept: "m:90+t:3", region: "m:90+t:1" };
    const fieldMap: any = { today: "f62", "5day": "f164", "10day": "f174" };
    const statMap: any = { today: "1", "5day": "5", "10day": "10" };
    const fields = "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f204,f205,f124,f164,f165,f174,f175";
    
    const url = "http://push2.eastmoney.com/api/qt/clist/get";
    const params = {
      pn: 1, pz: 500, po: 1,
      fid: fieldMap[indicator],
      fs: fsMap[sectorType],
      fields: fields,
      _: Date.now(),
    };

    const resp = await axios.get(url, { params, headers: EM_HEADERS, timeout: 8000, proxy: false });
    const data = resp.data?.data?.diff;
    
    if (data && data.length > 0) {
      data.forEach((d: any) => {
        dbCodeToName[d.f12] = d.f14;
      });
      const netInflowField = indicator === '5day' ? 'f164' : (indicator === '10day' ? 'f174' : 'f62');
      const netRatioField = indicator === '5day' ? 'f165' : (indicator === '10day' ? 'f175' : 'f184');

      return data.map((d: any) => ({
        code: d.f12,
        name: d.f14,
        change_pct: d.f3,
        main_net_inflow: d[netInflowField],
        main_net_ratio: d[netRatioField],
        super_large_net_inflow: d.f66,
        super_large_net_ratio: d.f69,
        large_net_inflow: d.f72,
        large_net_ratio: d.f75,
        medium_net_inflow: d.f78,
        medium_net_ratio: d.f81,
        small_net_inflow: d.f84,
        small_net_ratio: d.f87,
        top_stock_name: d.f204,
        top_stock_code: d.f205,
        update_time: d.f124,
      })).sort((a: any, b: any) => (b.main_net_inflow || 0) - (a.main_net_inflow || 0));
    }
  } catch (e) {
    console.warn("Eastmoney snapshot fetch failed, falling back to Sina...", e);
  }

  // HTSC SKILL FALLBACK (Priority for 5day/10day when Eastmoney fails)
  if (indicator === '5day' || indicator === '10day') {
    try {
      console.log(`[HTSC Skill] Fetching 5day/10day snapshot for indicator ${indicator}...`);
      const query = `行业板块${indicator === '5day' ? '5日' : '10日'}累计主力资金净流入排行，输出包含板块名称、代码、净流入金额、涨跌幅的JSON数据列表，直接输出JSON`;
      const htList = await callHtscIndicator(query);
      if (htList && Array.isArray(htList)) {
        console.log(`[HTSC Skill] 5day/10day snapshot fetch success, items count: ${htList.length}`);
        return htList.map((item: any) => {
          const name = item.板块名称 || item.板块 || item.name || "";
          const code = sectorNameToSinaCode[name] || `ht_${Buffer.from(name).toString('hex').substring(0, 8)}`;
          dbCodeToName[code] = name;
          
          const changePct = parseFloat(item.涨跌幅 || item.涨跌比率 || item.change_pct || "0");
          const mainNetInflow = parseMoney(item.净流入金额 || item.净流入 || item.net_inflow || "0");
          
          return {
            code,
            name,
            change_pct: changePct,
            main_net_inflow: mainNetInflow,
            main_net_ratio: 0.0,
            super_large_net_inflow: 0.0,
            super_large_net_ratio: 0.0,
            large_net_inflow: 0.0,
            large_net_ratio: 0.0,
            medium_net_inflow: 0.0,
            medium_net_ratio: 0.0,
            small_net_inflow: 0.0,
            small_net_ratio: 0.0,
            top_stock_name: '-',
            top_stock_code: '-',
            update_time: new Date().toISOString(),
          };
        });
      }
    } catch (err) {
      console.warn("[HTSC Skill] snapshot fallback failed:", err);
    }
  }

  // SINA FALLBACK
  try {
    const fenlei = sectorType === 'industry' ? '0' : '1';
    const url = `http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_bkzj_bk?page=1&num=100&sort=netamount&asc=0&fenlei=${fenlei}`;
    const resp = await axios.get(url, { headers: SINA_HEADERS, timeout: 3000, proxy: false });
    const sectors = resp.data;
    if (!Array.isArray(sectors)) return [];

    sectors.forEach((item: any) => {
      dbCodeToName[item.category] = item.name;
      sectorNameToSinaCode[item.name] = item.category;
    });

    return sectors.map((item: any) => ({
      code: item.category,
      name: item.name,
      change_pct: parseFloat(item.avg_changeratio) * 100,
      main_net_inflow: parseFloat(item.netamount),
      main_net_ratio: parseFloat(item.ratioamount) * 100,
      super_large_net_inflow: 0.0,
      super_large_net_ratio: 0.0,
      large_net_inflow: 0.0,
      large_net_ratio: 0.0,
      medium_net_inflow: 0.0,
      medium_net_ratio: 0.0,
      small_net_inflow: 0.0,
      small_net_ratio: 0.0,
      top_stock_name: item.ts_name || '-',
      top_stock_code: item.ts_symbol || '-',
      update_time: new Date().toISOString(),
    })).sort((a: any, b: any) => (b.main_net_inflow || 0) - (a.main_net_inflow || 0));
  } catch (e) {
    console.error("Sina snapshot fetch failed too:", e);
    return [];
  }
}

export async function fetchKline(code: string, indicator = 'today') {
  try {
    const isDaily = indicator === '5day' || indicator === '10day';
    const limit = indicator === '5day' ? 5 : (indicator === '10day' ? 10 : 0);
    
    const url = isDaily 
      ? "http://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
      : "http://push2.eastmoney.com/api/qt/stock/fflow/kline/get";

    const params = {
      secid: `90.${code}`,
      fields1: "f1,f2,f3,f7",
      fields2: "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
      lmt: limit,
      klt: isDaily ? 101 : 1,
      _: Date.now(),
    };
    
    const resp = await axios.get(url, { params, headers: EM_HEADERS, timeout: 8000, proxy: false });
    const klinesData = resp.data?.data?.klines;
    
    if (klinesData && klinesData.length > 0) {
      return klinesData.map((line: string) => {
        const parts = line.split(',');
        return {
          timestamp: parts[0],
          main_net_inflow: parseFloat(parts[1]),
          small_net_inflow: parseFloat(parts[2]),
          medium_net_inflow: parseFloat(parts[3]),
          large_net_inflow: parseFloat(parts[4]),
          super_large_net_inflow: parseFloat(parts[5]),
        };
      });
    }
  } catch (e) {
    console.warn(`Eastmoney kline fetch failed for ${code}, indicator ${indicator}, falling back to Sina...`);
  }

  // HTSC SKILL KLINE FALLBACK (Priority for 5day/10day when Eastmoney fails)
  if (indicator === '5day' || indicator === '10day') {
    try {
      const name = dbCodeToName[code] || code;
      console.log(`[HTSC Skill] Fetching 5day/10day K-lines for sector ${name} (${code})...`);
      const query = `行业板块'${name}'在最近${indicator === '5day' ? '5' : '10'}个交易日每天的主力资金净流入，以JSON格式输出列表，直接输出JSON`;
      const htKlines = await callHtscIndicator(query);
      if (htKlines && Array.isArray(htKlines)) {
        console.log(`[HTSC Skill] K-lines fetch success for ${name}`);
        return htKlines.map((item: any) => ({
          timestamp: item.date || item.日期 || item.timestamp || "",
          main_net_inflow: parseMoney(item.net_inflow || item.净流入 || item.main_net_inflow || "0"),
          small_net_inflow: 0.0,
          medium_net_inflow: 0.0,
          large_net_inflow: 0.0,
          super_large_net_inflow: 0.0,
        })).sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime());
      }
    } catch (err) {
      console.warn(`[HTSC Skill] kline fallback failed for ${code}:`, err);
    }
  }

  // SINA KLINE FALLBACK
  try {
    let sinaCode = code;
    if (!code.startsWith('new_')) {
      const name = dbCodeToName[code];
      if (name && sectorNameToSinaCode[name]) {
        sinaCode = sectorNameToSinaCode[name];
      }
    }
    const url = `http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssx_bkzj_fszs?page=1&num=241&bankuai=${sinaCode}`;
    const resp = await axios.get(url, { headers: SINA_HEADERS, timeout: 3000, proxy: false });
    const rawData = resp.data;
    if (!Array.isArray(rawData) || rawData.length < 2) return [];
    
    const ticks = rawData[1];
    const results: any[] = [];
    for (const item of ticks) {
      if (["14:58:00", "14:59:00", "15:00:00"].includes(item.ticktime)) continue;
      results.push({
        timestamp: `${item.opendate} ${item.ticktime}`,
        main_net_inflow: parseFloat(item.netamount),
        small_net_inflow: 0.0,
        medium_net_inflow: 0.0,
        large_net_inflow: 0.0,
        super_large_net_inflow: 0.0,
      });
    }
    // Sina returns ticks in reverse chronological order (newest first). We must sort them oldest to newest.
    return results.sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime());
  } catch (e) {
    console.error(`Sina kline fetch failed for ${code}:`, e);
    return [];
  }
}

async function callHtscIndicator(query: string): Promise<any> {
  const skillPath = "/Users/weiwang/Projects/stockview-next/.agents/skills/query-indicator/query_indicator.py";
  const cmd = `python3 "${skillPath}" queryIndicator --query "${query.replace(/"/g, '\\"')}"`;
  
  const env = {
    ...process.env,
    HT_APIKEY: "ht_ZcaFXhNnUdTRobeouZ8dbl7Xtz3ldenGPQvE6MA9I"
  };

  try {
    const { stdout } = await execPromise(cmd, { env });
    const result = JSON.parse(stdout);
    if (result.ok && result.data && result.data.answer) {
      const answer = result.data.answer;
      const match = answer.match(/```json([\s\S]*?)```/);
      const jsonStr = match ? match[1].trim() : answer.trim();
      return JSON.parse(jsonStr);
    }
  } catch (err) {
    console.error("Failed to query Htsc indicator:", err);
  }
  return null;
}
