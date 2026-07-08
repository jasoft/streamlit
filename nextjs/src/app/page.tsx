"use client";

import { useState, useMemo, useEffect, useRef } from 'react';
import useSWR from 'swr';
import ReactECharts from 'echarts-for-react';
import styles from './page.module.css';

const fetcher = (url: string) => fetch(url).then(res => res.json());

export default function FundFlowPage() {
  const [sectorType, setSectorType] = useState('industry');
  const [indicator, setIndicator] = useState('today');
  const [topN, setTopN] = useState(6);
  const [refreshInterval, setRefreshInterval] = useState(15);
  const [isLoaded, setIsLoaded] = useState(false);
  
  const chartRef = useRef<any>(null);
  const mouseRef = useRef({ x: 0, y: 0 });
  const closestSeriesIndexRef = useRef<number>(-1);
  const [showTable, setShowTable] = useState(false);

  // Local time state to force re-renders if needed, though SWR handles updates.
  const [now, setNow] = useState('');
  
  useEffect(() => {
    setNow(new Date().toLocaleTimeString());
    const timer = setInterval(() => setNow(new Date().toLocaleTimeString()), 1000);
    return () => clearInterval(timer);
  }, []);

  // 1. 从 localStorage 恢复配置
  useEffect(() => {
    if (typeof window !== 'undefined') {
      const savedSector = localStorage.getItem('stockview_sector_type');
      if (savedSector) setSectorType(savedSector);
      
      const savedIndicator = localStorage.getItem('stockview_indicator');
      if (savedIndicator) setIndicator(savedIndicator);
      
      const savedTopN = localStorage.getItem('stockview_top_n');
      if (savedTopN) {
        const val = parseInt(savedTopN, 10);
        if (!isNaN(val)) setTopN(val);
      }
      
      const savedInterval = localStorage.getItem('stockview_refresh_interval');
      if (savedInterval) {
        const val = parseInt(savedInterval, 10);
        if (!isNaN(val)) setRefreshInterval(val);
      }
      setIsLoaded(true);
    }
  }, []);

  // 2. 当配置改变时，自动保存到 localStorage
  useEffect(() => {
    if (isLoaded) {
      localStorage.setItem('stockview_sector_type', sectorType);
      localStorage.setItem('stockview_indicator', indicator);
      localStorage.setItem('stockview_top_n', topN.toString());
      localStorage.setItem('stockview_refresh_interval', refreshInterval.toString());
    }
  }, [sectorType, indicator, topN, refreshInterval, isLoaded]);

  const { data, error, isValidating } = useSWR(
    isLoaded ? `/api/fund-flow?type=${sectorType}&indicator=${indicator}&top_n=${topN}` : null,
    fetcher,
    { refreshInterval: refreshInterval * 1000, revalidateOnFocus: true }
  );

  const chartOptions = useMemo(() => {
    if (!data || !data.klines) return {};
    const series = [];
    const legendData = [];
    let xAxisData: string[] = [];
    
    for (const [name, klines] of Object.entries(data.klines)) {
      const typedKlines = klines as any[];
      if (!typedKlines || typedKlines.length === 0) continue;
      
      if (xAxisData.length === 0) {
        xAxisData = typedKlines.map(k => {
          if (k.timestamp.includes(' ')) {
            const timePart = k.timestamp.split(' ')[1];
            return timePart.substring(0, 5); // Ensure "HH:MM"
          }
          return k.timestamp.length >= 10 ? k.timestamp.substring(5, 10) : k.timestamp; // Format "2026-06-30" to "06-30"
        });
      }
      
      const yData = typedKlines.map(k => k.main_net_inflow / 100000000); // 亿元
      
      legendData.push(name);
      series.push({
        name,
        type: 'line',
        data: yData,
        smooth: true,
        symbol: 'circle',
        symbolSize: 3,
        showSymbol: false,
        emphasis: {
          focus: 'series',
          symbolSize: 8
        },
        lineStyle: { width: 2 },
      });
    }

    return {
      backgroundColor: 'transparent',
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'line' },
        formatter: function(params: any) {
          try {
            const closestIndex = closestSeriesIndexRef.current;
            let closestParam = params.find((p: any) => p.seriesIndex === closestIndex);
            if (!closestParam && params.length > 0) {
              closestParam = params[0];
            }
            if (!closestParam) return 'No closest series';
            
            const valueColor = closestParam.data >= 0 ? '#ef4444' : '#10b981';
            return `<b style="color:${closestParam.color}">${closestParam.seriesName}</b><br/>
                    时间: ${closestParam.name}<br/>
                    主力净流入: <b style="color:${valueColor}">${closestParam.data.toFixed(2)} 亿</b>`;
          } catch (err: any) {
            return `Error: ${err.message || String(err)}`;
          }
        }
      },
      legend: {
        data: legendData,
        textStyle: { color: '#94a3b8' },
        top: 0
      },
      grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true, top: 40 },
      xAxis: {
        type: 'category',
        boundaryGap: false,
        data: xAxisData,
        axisLine: { lineStyle: { color: '#334155' } },
        axisLabel: { color: '#94a3b8' }
      },
      yAxis: {
        type: 'value',
        axisLine: { show: false },
        axisLabel: { color: '#94a3b8', formatter: '{value} 亿' },
        splitLine: { lineStyle: { color: '#1e293b', type: 'dashed' } }
      },
      series
    };
  }, [data]);

  return (
    <div className={styles.container}>
      <aside className={styles.sidebar}>
        <h1 className={styles.title}>StockView Pro</h1>
        
        <div className={styles.controlGroup}>
          <label>板块类型</label>
          <select className={styles.select} value={sectorType} onChange={e => setSectorType(e.target.value)}>
            <option value="industry">行业板块</option>
            <option value="concept">概念板块</option>
            <option value="region">地域板块</option>
          </select>
        </div>
        
        <div className={styles.controlGroup}>
          <label>统计周期</label>
          <select className={styles.select} value={indicator} onChange={e => setIndicator(e.target.value)}>
            <option value="today">今日净流入</option>
            <option value="5day">5日累计净流入</option>
            <option value="10day">10日累计净流入</option>
          </select>
        </div>
        
        <div className={styles.controlGroup}>
          <label>对比展示个数 (流入/流出各 N 个)</label>
          <input 
            type="range" 
            className={styles.slider} 
            min={3} max={20} step={1}
            value={topN} 
            onChange={e => setTopN(parseInt(e.target.value, 10))} 
          />
          <div className={styles.sliderValue}>当前: 前后各 {topN} 个 (共 {topN * 2} 条线)</div>
        </div>

        <div className={styles.controlGroup}>
          <label>数据刷新频率 (秒)</label>
          <input 
            type="range" 
            className={styles.slider} 
            min={5} max={120} step={5}
            value={refreshInterval} 
            onChange={e => setRefreshInterval(parseInt(e.target.value, 10))} 
          />
          <div className={styles.sliderValue}>当前: {refreshInterval} 秒自动刷新</div>
        </div>
      </aside>

      <main className={styles.main}>
        <div 
          className={styles.chartCard} 
          style={{ position: 'relative' }}
          onMouseMove={(e) => {
            try {
              if (chartRef.current && data && data.klines) {
                const chart = chartRef.current.getEchartsInstance();
                
                // 防御性检查：确保 ECharts 选项和 X 轴已初始化，避免 convertFromPixel 抛出未找到坐标系的警告
                const option = chart.getOption();
                if (!option || !option.xAxis || (Array.isArray(option.xAxis) && option.xAxis.length === 0)) {
                  return;
                }

                const rect = chart.getDom().getBoundingClientRect();
                const x = e.clientX - rect.left;
                const y = e.clientY - rect.top;
                mouseRef.current = { x, y };

                // Convert X coordinate to data index
                const rawIndex = chart.convertFromPixel({ xAxisIndex: 0 }, x);
                
                // 限制 dataIndex 的上限，防止越界导致 [ECharts] Unknown dataType undefined 错误
                const klinesList = Object.entries(data.klines);
                let maxLen = 0;
                if (klinesList.length > 0) {
                  const firstKlines = klinesList[0][1] as any[];
                  if (firstKlines) maxLen = firstKlines.length;
                }
                const dataIndex = Math.min(Math.max(0, Math.round(rawIndex)), maxLen > 0 ? maxLen - 1 : 0);
                
                let closestIndex = -1;
                let minDiff = Infinity;
                
                klinesList.forEach(([name, klines]: any, index) => {
                  if (klines && klines[dataIndex]) {
                    const val = klines[dataIndex].main_net_inflow / 100000000;
                    const pixelY = chart.convertToPixel({ yAxisIndex: 0 }, val);
                    const diff = Math.abs(pixelY - y);
                    if (diff < minDiff) {
                      minDiff = diff;
                      closestIndex = index;
                    }
                  }
                });
                
                closestSeriesIndexRef.current = closestIndex;
                
                try {
                  for (let idx = 0; idx < klinesList.length; idx++) {
                    if (idx !== closestIndex) {
                      const seriesKlines = klinesList[idx][1] as any[];
                      const seriesLen = seriesKlines ? seriesKlines.length : 0;
                      if (seriesLen > 0 && dataIndex < seriesLen) {
                        chart.dispatchAction({
                          type: 'downplay',
                          seriesIndex: idx,
                          dataIndex: dataIndex
                        });
                      }
                    }
                  }
                  if (closestIndex !== -1) {
                    const closestKlines = klinesList[closestIndex][1] as any[];
                    const closestLen = closestKlines ? closestKlines.length : 0;
                    if (closestLen > 0 && dataIndex < closestLen) {
                      chart.dispatchAction({
                        type: 'highlight',
                        seriesIndex: closestIndex,
                        dataIndex: dataIndex
                      });
                    }
                  }
                } catch (err) {}
              }
            } catch (err) { }
          }}
          onMouseLeave={() => {
            try {
              if (chartRef.current) {
                const chart = chartRef.current.getEchartsInstance();
                const lastClosest = closestSeriesIndexRef.current;
                if (lastClosest !== -1) {
                  chart.dispatchAction({ 
                    type: 'downplay',
                    seriesIndex: lastClosest
                  });
                }
              }
            } catch (err) {}
          }}
        >
          <div className={styles.headerRow}>
            <h2 className={styles.cardTitle}>实时板块资金流向走势</h2>
            <div className={styles.statusIndicator}>
              {isValidating ? (
                <><div className={`${styles.dot} ${styles.fetching}`} /> 正在获取最新数据...</>
              ) : data?.is_stale ? (
                <><div className={`${styles.dot} ${styles.stale}`} /> 数据可能已过期，即将刷新...</>
              ) : (
                <><div className={styles.dot} /> 数据已是最新 (最后更新: {data?.update_time ? new Date(data.update_time).toLocaleTimeString() : now})</>
              )}
            </div>
          </div>
          
          {!data && !error && (
             <div className={styles.loadingOverlay}>首次连接服务器，正在建立本地数据库...</div>
          )}
          
          {error && <div style={{ color: '#ef4444' }}>获取数据失败，请重试</div>}
          
          {data && data.klines && Object.keys(data.klines).length > 0 && (
            <ReactECharts 
              ref={chartRef}
              option={chartOptions} 
              style={{ height: 650, width: '100%' }}
              notMerge={true}
              lazyUpdate={true}
            />
          )}
        </div>

        <div className={styles.tableCard}>
          <div 
            className={styles.headerRow} 
            style={{ cursor: 'pointer', userSelect: 'none', marginBottom: showTable ? 16 : 0 }} 
            onClick={() => setShowTable(!showTable)}
          >
            <h2 className={styles.cardTitle}>
              板块排行明细 {showTable ? '▲' : '▼'}
            </h2>
            <span style={{ fontSize: 14, color: '#94a3b8' }}>
              点击{showTable ? '折叠' : '展开'}排行数据
            </span>
          </div>
          {showTable && (
            <div style={{ overflowX: 'auto' }}>
              <table className={styles.table}>
                <thead>
                  <tr>
                    <th>排名</th>
                    <th>板块名称</th>
                    <th>涨跌幅</th>
                    <th>主力净流入(亿)</th>
                    <th>主力占比</th>
                    <th>领涨股</th>
                  </tr>
                </thead>
                <tbody>
                  {data?.snapshot?.map((row: any, index: number) => {
                    const inflowYi = (row.main_net_inflow / 100000000).toFixed(2);
                    const isPositive = row.main_net_inflow >= 0;
                    const changePct = typeof row.change_pct === 'number' ? row.change_pct.toFixed(2) : row.change_pct;
                    const netRatio = typeof row.main_net_ratio === 'number' ? row.main_net_ratio.toFixed(2) : row.main_net_ratio;
                    return (
                      <tr key={row.code}>
                        <td>{index + 1}</td>
                        <td style={{ fontWeight: 600 }}>{row.name}</td>
                        <td className={row.change_pct >= 0 ? styles.positive : styles.negative}>
                          {changePct}%
                        </td>
                        <td className={isPositive ? styles.positive : styles.negative}>
                          {inflowYi}
                        </td>
                        <td className={isPositive ? styles.positive : styles.negative}>
                          {netRatio}%
                        </td>
                        <td>{row.top_stock_name || '-'}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
