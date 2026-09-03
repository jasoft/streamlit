import { NextResponse } from 'next/server';
import { getDb } from '@/lib/db';
import { fetchSnapshot, fetchKline } from '@/lib/eastmoney';

const REFRESH_INTERVAL = 15000; // 15 seconds

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const sectorType = searchParams.get('type') || 'industry';
  const indicator = searchParams.get('indicator') || 'today';
  const topN = parseInt(searchParams.get('top_n') || '6', 10);
  
  const db = await getDb();
  const snapKey = `${sectorType}_${indicator}`;
  const cachedSnap = await db.get('SELECT * FROM snapshot_cache WHERE id = ?', [snapKey]);
  
  let snapshotData: any = null;
  let isSnapStale = true;
  
  if (cachedSnap) {
    try {
      snapshotData = JSON.parse(cachedSnap.data);
      isSnapStale = (Date.now() - cachedSnap.updated_at) > REFRESH_INTERVAL;
    } catch (e) {
      snapshotData = null;
    }
  }
  
  const updateDataAsync = async () => {
    const startTime = Date.now();
    console.log(`[API Route] Starting database cache update for ${snapKey}...`);
    try {
      const rows = await fetchSnapshot(sectorType, indicator);
      if (rows && rows.length > 0) {
        await db.run('INSERT OR REPLACE INTO snapshot_cache (id, data, updated_at) VALUES (?, ?, ?)', 
          [snapKey, JSON.stringify(rows), Date.now()]);
          
        const topRows = rows.slice(0, topN);
        const bottomRows = rows.slice(-topN);
        const targetCodes = [...topRows, ...bottomRows].map((r: any) => r.code);
        const targetNames = [...topRows, ...bottomRows].map((r: any) => r.name);
        
        console.log(`[API Route] Snapshot updated, now fetching K-lines for ${targetCodes.length} sectors...`);
        
        await Promise.all(targetCodes.map(async (code: string, i: number) => {
          const kStart = Date.now();
          try {
            console.log(`[API Route] Fetching K-line for ${targetNames[i]} (${code})...`);
            const kline = await fetchKline(code, indicator);
            if (kline && kline.length > 0) {
              const klineKey = `${code}_${indicator}`;
              await db.run('INSERT OR REPLACE INTO kline_cache (id, data, updated_at) VALUES (?, ?, ?)',
                [klineKey, JSON.stringify({ name: targetNames[i], kline }), Date.now()]);
              console.log(`[API Route] K-line updated for ${targetNames[i]} in ${Date.now() - kStart}ms`);
            } else {
              console.warn(`[API Route] Empty K-line for ${targetNames[i]} (${code})`);
            }
          } catch(e) {
            console.error(`[API Route] Failed kline for ${code}:`, e);
          }
        }));
      }
      console.log(`[API Route] Cache update for ${snapKey} completed in ${Date.now() - startTime}ms`);
    } catch(e) {
      console.error("[API Route] Async background update failed:", e);
    }
  };
  
  if (!snapshotData || snapshotData.length === 0) {
    // Blocking fetch if no cache exists, with a 10s maximum timeout to prevent frontend freezing
    console.log(`[API Route] No cache found for ${snapKey}. Initiating blocking fetch...`);
    const timeoutPromise = new Promise((resolve) => setTimeout(resolve, 10000));
    await Promise.race([updateDataAsync(), timeoutPromise]);
    
    const updatedSnap = await db.get('SELECT * FROM snapshot_cache WHERE id = ?', [snapKey]);
    if (updatedSnap) {
      snapshotData = JSON.parse(updatedSnap.data);
    }
  } else if (isSnapStale) {
    // Non-blocking fetch in background
    updateDataAsync(); // fire and forget
  }
  
  if (!snapshotData) {
    return NextResponse.json({ snapshot: [], klines: {} }, { status: 500 });
  }
  
  const topRows = snapshotData.slice(0, topN);
  const bottomRows = snapshotData.slice(-topN);
  const targetCodes = [...topRows, ...bottomRows].map((r: any) => r.code);
  
  const klinesResult: Record<string, any> = {};
  for (const code of targetCodes) {
    const klineKey = `${code}_${indicator}`;
    const cachedKline = await db.get('SELECT * FROM kline_cache WHERE id = ?', [klineKey]);
    if (cachedKline) {
      try {
        const parsed = JSON.parse(cachedKline.data);
        klinesResult[parsed.name] = parsed.kline;
      } catch(e) { }
    }
  }
  
  return NextResponse.json({
    snapshot: snapshotData,
    klines: klinesResult,
    is_stale: isSnapStale,
    update_time: Date.now()
  });
}
