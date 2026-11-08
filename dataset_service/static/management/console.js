(() => {
  'use strict';
  const TOKEN_KEY = 'deqing7110_admin_session';
  const token = () => sessionStorage.getItem(TOKEN_KEY) || '';
  const state = { items: [], warnings: [], categories: {}, selectedCategory: 'all', extentFilter: 'all', query: '', bboxFilter: null, cardView: false, activePage: 'catalog', pendingUploads: {} };
  const $ = (id) => document.getElementById(id);
  const CAT = {
    imagery: { label: '影像', icon: '▧', css: 'imagery' }, raster: { label: '栅格', icon: '▦', css: 'raster' },
    vector: { label: '矢量', icon: '⌁', css: 'vector' }, model: { label: '三维模型', icon: '⬡', css: 'model' },
    pointcloud: { label: '点云', icon: '⁙', css: 'pointcloud' }, video: { label: '视频', icon: '▷', css: 'video' },
    text: { label: '文本', icon: '≡', css: 'other' }, api: { label: '服务接口', icon: '⌘', css: 'other' }, other: { label: '其他', icon: '◈', css: 'other' }
  };
  const E = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  const bytes = (n) => {
    n = Number(n); if (!Number.isFinite(n) || n <= 0) return '—';
    const units = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(i >= 3 ? 2 : i === 2 ? 1 : 0)} ${units[i]}`;
  };
  const dateText = (v) => {
    if (!v) return '—'; const d = new Date(v); return Number.isNaN(d.getTime()) ? String(v).slice(0, 10) : d.toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' });
  };
  const parseJSON = (v) => { if (v && typeof v === 'object') return v; if (typeof v !== 'string') return {}; try { return JSON.parse(v); } catch { return {}; } };
  function redact(value) {
    if (Array.isArray(value)) return value.map(redact);
    if (!value || typeof value !== 'object') return value;
    const result = {};
    for (const [key, child] of Object.entries(value)) {
      if (/(password|passwd|secret|authorization|access.?token|refresh.?token)/i.test(key)) result[key] = child ? '••••••' : child;
      else result[key] = redact(child);
    }
    return result;
  }
  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (token()) headers.set('Authorization', `Bearer ${token()}`);
    if (typeof options.body === 'string' && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
    const response = await fetch(path, { ...options, headers, cache: 'no-store' });
    const contentType = response.headers.get('content-type') || '';
    const body = contentType.includes('json') ? await response.json() : await response.text();
    if (!response.ok) {
      if (response.status === 401) { sessionStorage.removeItem(TOKEN_KEY); showLogin(); }
      const message = typeof body === 'object' ? (body.detail || body.Message || `HTTP ${response.status}`) : (body || `HTTP ${response.status}`);
      throw new Error(String(message));
    }
    return body;
  }
  function showLogin(message = '') {
    $('login-view').hidden = false; $('app-view').hidden = true;
    $('login-error').textContent = message;
    document.querySelector('.topbar').hidden = true;
  }
  function showApp(user) {
    $('login-view').hidden = true; $('app-view').hidden = false;
    document.querySelector('.topbar').hidden = false;
    $('user-name').textContent = user.RealName || user.UserName || '管理员';
  }
  function toast(message) {
    const node = $('toast'); node.textContent = message; node.classList.add('show');
    clearTimeout(toast.timer); toast.timer = setTimeout(() => node.classList.remove('show'), 2100);
  }
  function bboxFromGeometry(g) {
    if (!g || typeof g !== 'object') return null;
    if (g.type === 'Feature') return bboxFromGeometry(g.geometry);
    if (g.type === 'FeatureCollection') return mergeBoxes((g.features || []).map(f => bboxFromGeometry(f.geometry)));
    if (g.type === 'GeometryCollection') return mergeBoxes((g.geometries || []).map(bboxFromGeometry));
    const coords = g.coordinates; if (!Array.isArray(coords)) return null;
    let west = Infinity, south = Infinity, east = -Infinity, north = -Infinity;
    const walk = (node) => {
      if (!Array.isArray(node)) return;
      if (node.length >= 2 && Number.isFinite(Number(node[0])) && Number.isFinite(Number(node[1]))) {
        const x = Number(node[0]), y = Number(node[1]);
        if (x >= -180 && x <= 180 && y >= -90 && y <= 90) { west = Math.min(west, x); east = Math.max(east, x); south = Math.min(south, y); north = Math.max(north, y); }
      } else node.forEach(walk);
    };
    walk(coords); return validBox([west, south, east, north]) ? [west, south, east, north] : null;
  }
  function mergeBoxes(boxes) {
    const valid = boxes.filter(Boolean); if (!valid.length) return null;
    return [Math.min(...valid.map(b => b[0])), Math.min(...valid.map(b => b[1])), Math.max(...valid.map(b => b[2])), Math.max(...valid.map(b => b[3]))];
  }
  function validBox(b) { return Array.isArray(b) && b.length === 4 && b.every(Number.isFinite) && b[0] <= b[2] && b[1] <= b[3] && b[0] >= -180 && b[2] <= 180 && b[1] >= -90 && b[3] <= 90; }
  function explicitBox(meta) {
    const candidates = [meta?.spatial_extent_bbox_wgs84, meta?.bbox_wgs84, meta?.extent_wgs84, meta?.spatial_extent];
    for (const value of candidates) {
      if (Array.isArray(value) && value.length === 4) { const b = value.map(Number); if (validBox(b)) return b; }
      if (value && typeof value === 'object') { const b = bboxFromGeometry(value); if (b) return b; }
    }
    return null;
  }
  function parseLegacyRange(value) {
    if (!value || typeof value !== 'string') return null;
    const chunks = value.replace(/[()]/g, '').split(',');
    const pairs = chunks.map(chunk => chunk.match(/[-+]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g) || [])
      .filter(nums => nums.length >= 2).map(nums => [Number(nums[0]), Number(nums[1])]);
    if (!pairs.length || pairs.some(([x, y]) => Math.abs(x) > 180 || Math.abs(y) > 90)) return null;
    const b = [Math.min(...pairs.map(p => p[0])), Math.min(...pairs.map(p => p[1])), Math.max(...pairs.map(p => p[0])), Math.max(...pairs.map(p => p[1]))];
    return validBox(b) ? b : null;
  }
  function classifyLegacy(row) {
    const type = Number(row.Type), name = String(row.TypeName || '').toLowerCase(), label = String(row.Label || '').toLowerCase();
    if (type === 4000001 || name.includes('倾斜') || label.includes('倾斜影像')) return 'imagery';
    if (type === 1000012 || /terrain|mesh|model/.test(name)) return 'model';
    if (type === 13000006 || name.includes('pointcloud') || name.includes('点云')) return 'pointcloud';
    if (type === 10000004 || name.includes('视频')) return 'video';
    if ([12000008, 12000004, 12000005].includes(type) || /raster|wmts|wms|tms|栅格/.test(name)) return 'raster';
    if (type === 2000004 || name.includes('wfs') || name.includes('矢量')) return 'vector';
    return 'other';
  }
  function makeLegacy(row) {
    const metadata = parseJSON(row.MetaData); const category = classifyLegacy(row);
    const bbox = explicitBox(metadata) || explicitBox(row) || parseLegacyRange(row.Range);
    const srs = row.SrsDefinition || metadata.SRS || (row.EpsgId ? `EPSG:${row.EpsgId}` : '');
    const explicit = Boolean(explicitBox(metadata) || explicitBox(row));
    const declaredGeo = /4326|WGS ?84/i.test(String(srs));
    const canMap = bbox && (explicit || declaredGeo);
    let accessUrl = '';
    const id = String(row.Id || '');
    if (Number(row.Type) === 1000012) accessUrl = `/api/Mesh/GetMesh3DTilesFile/${encodeURIComponent(id)}/tileset.json`;
    else if (Number(row.Type) === 13000006) accessUrl = `/api/PointCload/GetPointCloud3DTilesFile/${encodeURIComponent(id)}/tileset.json`;
    else if (Number(row.Type) === 12000008) accessUrl = `/api/Raster/GetTmsFile/${encodeURIComponent(id)}/tilemapresource.xml`;
    else if (category === 'imagery') accessUrl = `/spatial/oblique-images?resource_id=${encodeURIComponent(id)}&limit=100`;
    else accessUrl = `/api/ArScene/GetSceneResourceByPage`;
    const accessKind = [1000012, 13000006, 12000008].includes(Number(row.Type)) ? 'legacy' : category === 'imagery' ? 'api' : 'reference';
    return {
      id, name: row.Label || row.Name || id, category, type_name: row.TypeName || CAT[category].label,
      format: metadata.StorageFormat || row.TypeKey || row.TypeName || '旧资源目录', source: '旧资源目录', size: Number(row.FileSize) || null,
      file_count: Number(row.FileCount) || null, srs, created_at: row.UploadTime || null, status: row.IsUploadCompleted === false ? '待完成' : '已登记',
      bbox: canMap ? bbox : null, raw_range: row.Range || null, bbox_confidence: explicit ? '登记为 WGS84' : declaredGeo && bbox ? '旧目录范围 · SRS 声明为 WGS84' : bbox ? '旧 Range 有坐标，但未确认为 WGS84' : '未登记 WGS84 范围',
      raw_bbox: bbox, metadata, description: row.Description || '', access_url: accessUrl, access_method: accessKind === 'reference' ? 'POST' : 'GET', access_kind: accessKind, legacy_type: Number(row.Type) || null,
      delete_kind: metadata.ResourceSubset ? 'subset' : null,
      rename_kind: metadata.ResourceSubset ? 'subset' : 'legacy',
      id_short: id.slice(0, 8), tags: row.DataSetId ? [`数据集 ${row.DataSetId}`] : []
    };
  }
  function modernItem({ id, name, category, typeName, format, source, size, sizeNote, count, srs, date, status, geometry, metadata, description, accessUrl, accessKind, raw }) {
    const rawArrayBox = Array.isArray(geometry) && geometry.length === 4 ? geometry.map(Number) : null;
    const bbox = (rawArrayBox && validBox(rawArrayBox) ? rawArrayBox : bboxFromGeometry(geometry)) || explicitBox(metadata);
    return { id: String(id || ''), name: name || String(id || '未命名'), category, type_name: typeName || CAT[category]?.label || category,
      format: format || '—', source, size: Number(size) || null, size_note: sizeNote || '', file_count: Number(count) || null, srs: srs || '', created_at: date || null,
      status: status || '已登记', bbox, raw_range: null, raw_bbox: bbox, bbox_confidence: bbox ? '空间数据表 · WGS84' : '未登记 WGS84 范围',
      metadata: metadata || {}, description: description || '', access_url: accessUrl || '', access_method: 'GET', access_kind: accessKind || 'api', raw,
      delete_kind: source === '视频组服务' ? 'video' : source === '空间数据库' ? 'vector' : source === '时序栅格服务' ? 'raster'
        : source === '影像索引 / PostGIS' ? 'imagery' : source === '点云版本库' ? 'pointcloud' : null,
      rename_kind: source === '视频组服务' ? 'video' : source === '空间数据库' ? 'vector' : source === '时序栅格服务' ? 'raster'
        : source === '影像索引 / PostGIS' ? 'imagery' : source === '点云版本库' ? 'pointcloud' : null };
  }
  async function loadCatalog() {
    $('result-count').textContent = '正在读取各类型数据源…'; $('resource-rows').innerHTML = '<tr><td colspan="6" class="empty">正在连接 7110 数据目录…</td></tr>';
    const jobs = [
      ['旧资源目录', () => api('/api/ArScene/GetSceneResourceByPage', { method: 'POST', body: JSON.stringify({ PageIndex: 1, PageSize: 1000 }) })],
      ['版本化点云与三维瓦片', () => api('/pointcloud/resources?limit=1000&offset=0')],
      ['视频资源', () => api('/videos/groups?limit=500&offset=0')],
      ['矢量数据', () => api('/spatial/datasets?limit=1000&offset=0')],
      ['POI 统一表', () => api('/spatial/pois/summary')],
      ['时序栅格', () => api('/spatial/raster-collections')],
      ['倾斜影像资源', () => api('/spatial/oblique-images/resources')]
    ];
    const results = await Promise.allSettled(jobs.map(j => j[1]()));
    const byName = new Map(results.map((r, i) => [jobs[i][0], r]));
    state.warnings = results.flatMap((r, i) => r.status === 'rejected' ? [`${jobs[i][0]}：${r.reason.message}`] : []);
    let items = [];
    const legacy = byName.get('旧资源目录');
    if (legacy?.status === 'fulfilled') {
      const rows = legacy.value.Rows || legacy.value.rows || [];
      items.push(...rows.map(makeLegacy));
    }
    const pc = byName.get('版本化点云与三维瓦片');
    if (pc?.status === 'fulfilled') {
      for (const resource of (pc.value.items || [])) {
        const versions = resource.versions || [];
        const published = versions.filter(v => v.status === 'published').sort((a, b) => String(b.published_at || b.created_at).localeCompare(String(a.published_at || a.created_at)));
        const latest = published[0] || versions[0] || {};
        const contentKind = String(resource.metadata?.content_kind || latest.metadata?.content_kind || '').toLowerCase();
        const category = latest.format === '3dtiles' ? (contentKind.includes('point') ? 'pointcloud' : (Number(items.find(i => i.id === resource.id)?.legacy_type) === 13000006 ? 'pointcloud' : 'model')) : 'pointcloud';
        const item = modernItem({ id: resource.id, name: resource.name, category, typeName: latest.format === '3dtiles' ? `${category === 'model' ? '3D Tiles 模型' : '3D Tiles 点云'}` : `点云 ${latest.format || ''}`,
          format: latest.format || 'pointcloud', source: '点云版本库', size: latest.total_bytes || resource.total_bytes, count: latest.file_count,
          srs: resource.srs, date: latest.published_at || resource.updated_at || resource.created_at,
          status: latest.status === 'published' ? '已发布' : latest.status || '已登记', geometry: resource.spatial_extent,
          metadata: { ...(resource.metadata || {}), version_count: versions.length, published_version: latest.id || null }, description: resource.description,
          accessUrl: latest.entrypoint_url || (latest.id ? `/pointcloud/versions/${latest.id}/manifest` : `/pointcloud/resources/${resource.id}`), accessKind: 'api', raw: resource });
        const legacyMatch = items.findIndex(i => i.id === item.id);
        if (legacyMatch >= 0) {
          const isBridgeRow = String(items[legacyMatch].metadata?.PointCloudResourceId || '') === item.id;
          items[legacyMatch] = { ...items[legacyMatch], ...item, category: items[legacyMatch].category === 'other' ? item.category : items[legacyMatch].category,
            source: '旧目录 + 点云版本库', legacy_type: items[legacyMatch].legacy_type, access_url: items[legacyMatch].access_url, access_kind: 'legacy',
            delete_kind: isBridgeRow ? 'pointcloud' : null,
            bbox: item.bbox || items[legacyMatch].bbox, raw_bbox: item.raw_bbox || items[legacyMatch].raw_bbox,
            bbox_confidence: item.bbox ? item.bbox_confidence : items[legacyMatch].bbox_confidence };
        } else items.push(item);
      }
    }
    const videos = byName.get('视频资源');
    if (videos?.status === 'fulfilled') for (const v of (videos.value.items || [])) {
      const item = modernItem({ id: v.id, name: v.name, category: 'video', typeName: '定向视频组', format: 'MP4 + XML/资产组', source: '视频组服务',
        size: v.total_bytes, count: v.asset_count, srs: v.source_srs, date: v.published_at || v.created_at, status: v.status,
        geometry: v.spatial_extent, metadata: v.metadata || {}, description: '', accessUrl: `/videos/groups/${v.id}`, accessKind: 'api', raw: v });
      const legacyMatch = items.findIndex(existing => existing.id === item.id);
      if (legacyMatch >= 0) items[legacyMatch] = { ...items[legacyMatch], ...item, source: '旧目录 + 视频组服务' };
      else items.push(item);
    }
    const vectors = byName.get('矢量数据');
    const poi = byName.get('POI 统一表');
    const poiSummary = poi?.status === 'fulfilled' ? poi.value : null;
    const poiSourceIds = new Set([...(poiSummary?.categories || []), ...(poiSummary?.supplemental_datasets || [])]
      .map(d => String(d.id).toLowerCase()));
    if (poiSummary) items.push(modernItem({ id: poiSummary.id, name: '全部 POI', category: 'vector',
      typeName: 'PostGIS POI 点', format: '单表 · 按 POI ID 去重', source: 'POI 统一目录',
      count: poiSummary.unique_poi_count, srs: 'EPSG:4326', status: '已入库',
      metadata: poiSummary, description: `${Number(poiSummary.unique_poi_count || 0).toLocaleString()} 个 POI 点，按范围或 ID 查询`,
      accessUrl: '/spatial/pois/summary', accessKind: 'api', raw: poiSummary }));
    if (vectors?.status === 'fulfilled') for (const d of (vectors.value.datasets || [])) {
      if (poiSourceIds.has(String(d.id).toLowerCase())) continue;
      items.push(modernItem({ id: d.id, name: d.name, category: 'vector', typeName: 'PostGIS 矢量数据', format: 'Shapefile → PostGIS', source: '空间数据库',
        size: d.storage_bytes, sizeNote: 'PostGIS 要素行数据估算', count: d.feature_count, srs: d.source_epsg ? `EPSG:${d.source_epsg}` : '已转换至 EPSG:4326', date: d.created_at, status: '已入库',
        geometry: d.bounds, metadata: { source_filename: d.source_filename, coordinate_dimension: d.coordinate_dimension, vertical_reference_status: d.vertical_reference_status, z_min: d.z_min, z_max: d.z_max, fields: d.fields },
        description: `${d.feature_count ?? 0} 个要素`, accessUrl: `/spatial/datasets/${d.id}/features?limit=100`, accessKind: 'api', raw: d }));
    }
    const rasters = await api('/spatial/raster-collections').catch(e => ({ collections: [], __error: e.message }));
    if (rasters.__error) state.warnings.push(`时序栅格：${rasters.__error}`);
    for (const c of (rasters.collections || [])) {
      items.push(modernItem({ id: c.id, name: c.name, category: 'raster', typeName: '时序栅格集合', format: `${c.tile_scheme || 'XYZ'} · ${c.tile_format || 'tiles'}`,
        source: '时序栅格服务', size: c.total_bytes, sizeNote: '已登记瓦片文件大小合计', count: c.tile_count, srs: `EPSG:${c.crs_epsg}`, date: c.created_at, status: '已登记', geometry: c.bounds, metadata: c,
        description: c.description, accessUrl: `/spatial/raster-collections/${c.id}`, accessKind: 'api', raw: c }));
    }
    const oblique = byName.get('倾斜影像资源');
    if (oblique?.status === 'fulfilled') for (const r of (oblique.value.resources || [])) {
      const item = modernItem({ id: r.id, name: r.name, category: 'imagery', typeName: '倾斜影像与相机位姿', format: 'JPEG + 相机参数',
        source: '影像索引 / PostGIS', size: r.total_bytes, sizeNote: '原始影像文件大小合计', count: r.image_count,
        srs: r.source_srs, date: r.indexed_at, status: r.location_review?.confirmed ? '位置已核验' : '待精度核验',
        geometry: r.camera_center_extent_bbox_wgs84,
        metadata: { location_review: r.location_review, image_count: r.image_count, no_footprint_count: r.no_footprint_count,
          camera_center_extent_bbox_wgs84: r.camera_center_extent_bbox_wgs84, absolute_accuracy: r.location_review?.absolute_accuracy || 'unknown' },
        description: `${Number(r.image_count || 0).toLocaleString('zh-CN')} 张影像；相机中心范围仅表示拍摄点分布，不代表影像地面覆盖范围。`,
        accessUrl: `/spatial/oblique-images?resource_id=${encodeURIComponent(r.id)}&limit=100`, accessKind: 'api', raw: r });
      item.bbox_confidence = '相机中心点包络范围，不代表影像覆盖范围';
      items.push(item);
    }
    // Raster listing is requested separately because older deployments may not yet expose it.
    items.sort((a, b) => a.category.localeCompare(b.category) || a.name.localeCompare(b.name, 'zh-CN'));
    const unique = new Map();
    for (const item of items) {
      const key = `${item.id}|${item.category}`;
      if (!unique.has(key)) unique.set(key, item);
      else unique.set(key, { ...unique.get(key), ...item });
    }
    state.items = [...unique.values()];
    render();
  }
  function visibleItems() {
    const q = state.query.trim().toLocaleLowerCase();
    return state.items.filter(item => (state.selectedCategory === 'all' || item.category === state.selectedCategory)
      && (state.extentFilter === 'all' || (state.extentFilter === 'yes') === Boolean(item.bbox))
      && (!state.bboxFilter || (item.bbox && item.bbox[0] <= state.bboxFilter[2] && item.bbox[2] >= state.bboxFilter[0] && item.bbox[1] <= state.bboxFilter[3] && item.bbox[3] >= state.bboxFilter[1]))
      && (!q || [item.name, item.id, item.type_name, item.srs, item.format, item.source].join(' ').toLocaleLowerCase().includes(q)));
  }
  function renderNav() {
    const counts = {}; for (const item of state.items) counts[item.category] = (counts[item.category] || 0) + 1;
    $('nav-count').textContent = state.items.length;
    const nav = $('type-nav'); nav.innerHTML = Object.entries(CAT).filter(([key]) => counts[key]).map(([key, cat]) => `<button data-category="${key}" class="${state.selectedCategory === key ? 'selected' : ''}"><i class="type-dot ${cat.css}"></i>${E(cat.label)}<span class="nav-count">${counts[key]}</span></button>`).join('');
    nav.querySelectorAll('[data-category]').forEach(b => b.onclick = () => { state.selectedCategory = b.dataset.category; $('category-filter').value = state.selectedCategory; render(); });
    const select = $('category-filter'); const current = select.value || 'all';
    select.innerHTML = `<option value="all">所有类型</option>` + Object.entries(CAT).filter(([key]) => counts[key]).map(([key, cat]) => `<option value="${key}">${E(cat.label)} (${counts[key]})</option>`).join('');
    select.value = counts[current] ? current : state.selectedCategory;
  }
  function renderStats() {
    const counts = {}; state.items.forEach(i => counts[i.category] = (counts[i.category] || 0) + 1);
    const totalBytes = state.items.reduce((a, x) => a + (Number(x.size) || 0), 0);
    const withExtent = state.items.filter(x => x.bbox).length;
    const warnings = state.warnings.length;
    const cards = [
      ['资源总数', state.items.length.toLocaleString('zh-CN'), `${Object.keys(counts).length} 种已登记类型`, '▦', '#e9f5ee', '#198052'],
      ['可空间查询', withExtent.toLocaleString('zh-CN'), `${state.items.length - withExtent} 条范围缺失或待核验`, '⌖', '#eaf3fa', '#438ab7'],
      ['登记资源体量', bytes(totalBytes), '含瓦片文件大小与 PostGIS 要素行估算', '▤', '#fbf2e6', '#bf7a35'],
      ['数据源状态', warnings ? `${warnings} 项待检查` : '连接正常', warnings ? '有接口未响应，详见刷新结果' : '目录接口均已响应', warnings ? '!' : '↻', warnings ? '#fff1e9' : '#f1eef8', warnings ? '#c16c3b' : '#7558a6']
    ];
    $('stats').innerHTML = cards.map(c => `<div class="stat-card" style="--tone:${c[4]};--tone-ink:${c[5]}"><div class="stat-top"><span>${c[0]}</span><i class="stat-icon">${c[3]}</i></div><div class="stat-value">${E(c[1])}</div><div class="stat-sub">${E(c[2])}</div></div>`).join('');
  }
  function formatExtent(item) {
    if (item.bbox) return `${item.category === 'imagery' ? '相机中心 ' : ''}${item.bbox.map(n => Number(n).toFixed(4)).join(', ')}`;
    if (item.raw_bbox) return '旧目录范围 · 未确认为 WGS84';
    return '未登记';
  }
  function renderRows() {
    const items = visibleItems(); $('result-count').textContent = `共 ${items.length} 条资源 · 可按名称、类型和空间范围筛选${state.warnings.length ? ` · ${state.warnings.length} 个数据接口未响应` : ''}`;
    $('result-count').title = state.warnings.join('\n');
    $('table-summary').textContent = `显示 ${items.length} / ${state.items.length} 条`;
    const tbody = $('resource-rows');
    if (!items.length) tbody.innerHTML = `<tr><td colspan="6" class="empty">${state.warnings.length ? '暂未从服务读取到目录数据。' : '没有符合当前筛选条件的资源。'}</td></tr>`;
    else tbody.innerHTML = items.map((item, ix) => {
      const cat = CAT[item.category] || CAT.other;
      return `<tr data-index="${ix}"><td><div class="resource-name"><i class="resource-icon ${cat.css}">${cat.icon}</i><span><span class="resource-label">${E(item.name)}</span><small class="resource-id">${E(item.id)}</small></span></div></td><td><span class="type-pill"><i class="type-dot ${cat.css}"></i>${E(item.type_name || cat.label)}</span></td><td class="size-cell">${bytes(item.size)}${item.size_note ? `<small>${E(item.size_note)}</small>` : ''}</td><td><span class="extent-cell"><i class="extent-mark ${item.bbox ? '' : 'none'}"></i>${item.bbox ? E(formatExtent(item)) : item.raw_bbox ? '待核验' : '未登记'}</span></td><td class="date-cell">${E(dateText(item.created_at))}</td><td><button class="row-open" aria-label="查看详情">›</button></td></tr>`;
    }).join('');
    tbody.querySelectorAll('tr[data-index]').forEach(tr => tr.addEventListener('click', () => openDetail(items[Number(tr.dataset.index)])));
    const cards = $('card-view'); cards.innerHTML = items.map((item, ix) => {
      const cat = CAT[item.category] || CAT.other;
      return `<article class="resource-card" data-index="${ix}"><div class="resource-name"><i class="resource-icon ${cat.css}">${cat.icon}</i><span><span class="resource-label">${E(item.name)}</span><small class="resource-id">${E(item.id)}</small></span></div><div class="card-meta"><span>${E(item.type_name || cat.label)}</span><span>${bytes(item.size)}</span></div></article>`;
    }).join('');
    cards.querySelectorAll('[data-index]').forEach(el => el.onclick = () => openDetail(items[Number(el.dataset.index)]));
    renderMap(items);
  }
  function renderMap(items) {
    const mapped = items.filter(item => item.bbox && item.bbox.every(Number.isFinite));
    const svg = $('extent-map');
    if (!mapped.length) { svg.innerHTML = ''; $('map-empty').hidden = false; return; }
    $('map-empty').hidden = true;
    const boxes = mapped.map(x => x.bbox); let west = Math.min(...boxes.map(b => b[0])), south = Math.min(...boxes.map(b => b[1])), east = Math.max(...boxes.map(b => b[2])), north = Math.max(...boxes.map(b => b[3]));
    const dx = Math.max(east - west, .01), dy = Math.max(north - south, .01); west -= dx * .08; east += dx * .08; south -= dy * .08; north += dy * .08;
    const sx = 352 / (east - west), sy = 212 / (north - south), scale = Math.min(sx, sy), ox = (400 - (east - west) * scale) / 2, oy = (260 - (north - south) * scale) / 2;
    svg.innerHTML = mapped.map((item, i) => {
      const b = item.bbox, x = ox + (b[0] - west) * scale, y = 260 - oy - (b[3] - south) * scale;
      const w = Math.max((b[2] - b[0]) * scale, 5), h = Math.max((b[3] - b[1]) * scale, 5), css = CAT[item.category]?.css || 'other';
      return `<rect class="map-extent ${css}" data-index="${i}" x="${Math.max(15, x)}" y="${Math.max(10, y)}" width="${Math.min(w, 370)}" height="${Math.min(h, 230)}" rx="3"><title>${E(item.name)} · ${E(formatExtent(item))}</title></rect>`;
    }).join('');
    svg.querySelectorAll('[data-index]').forEach(rect => rect.addEventListener('click', () => openDetail(mapped[Number(rect.dataset.index)])));
  }
  function render() {
    renderNav(); renderStats(); renderRows();
    $('category-filter').value = state.selectedCategory;
  }
  function openDetail(item) {
    const cat = CAT[item.category] || CAT.other;
    $('detail-title').textContent = item.name;
    const ext = item.bbox ? `<div class="extent-box">${E(item.bbox.map(n => Number(n).toFixed(7)).join(', '))}<br><span class="extent-confidence">${E(item.bbox_confidence)}</span></div>` : `<div class="extent-box">${item.raw_range ? `目录 Range：${E(item.raw_range)}` : '未登记可信 WGS84 空间范围'}<br><span class="extent-confidence">${E(item.bbox_confidence || '需要补充空间范围')}</span></div>`;
    const raw = item.raw ? item.raw : item.metadata;
    const actions = [];
    const deleteLabel = item.delete_kind === 'subset' ? '删除区域子资源' : item.delete_kind === 'imagery' ? '删除影像索引与本地文件' : '删除本地资源及文件';
    const deleteNote = item.delete_kind === 'subset'
      ? '该区域子资源是虚拟资源入口；删除只移除子资源登记和场景引用，不删除源瓦片。'
      : item.delete_kind === 'imagery'
        ? '删除影像索引和该资源独占的数据卷影像；旧共享镜像对象会保留。'
        : '删除会清除本地登记、场景引用和该资源独占的数据文件。旧镜像共享文件不会被删除。';
    if (item.access_kind === 'legacy' && item.access_url) actions.push(`<a class="detail-link" target="_blank" rel="noopener" href="${E(item.access_url)}">打开旧接口入口 ↗</a>`);
    if (item.access_kind === 'api' && item.access_url) actions.push(`<button class="detail-link" id="preview-endpoint">${item.category === 'vector' ? '读取前 100 个要素' : '读取接口响应'}</button>`);
    if (item.rename_kind) actions.push('<button class="detail-link" id="rename-resource" type="button">重命名</button>');
    if (item.delete_kind) actions.push(`<button class="detail-link delete-link" id="delete-resource">${deleteLabel}</button>`);
    $('detail-content').innerHTML = `
      <div class="detail-summary"><i class="resource-icon ${cat.css}">${cat.icon}</i><div><h3>${E(item.name)}</h3><p>${E(item.id)}</p></div></div>
      <div class="detail-section"><div class="kv-grid">
        <div class="kv"><span>资源类型</span><strong>${E(item.type_name || cat.label)}</strong></div><div class="kv"><span>存储格式</span><strong>${E(item.format || '—')}</strong></div>
        <div class="kv"><span>${E(item.size_note || '存储大小')}</span><strong>${bytes(item.size)}</strong></div><div class="kv"><span>文件 / 要素数</span><strong>${item.file_count == null ? '—' : Number(item.file_count).toLocaleString('zh-CN')}</strong></div>
        <div class="kv"><span>坐标系</span><strong>${E(item.srs || '未登记')}</strong></div><div class="kv"><span>状态</span><strong>${E(item.status || '已登记')}</strong></div>
        <div class="kv"><span>来源</span><strong>${E(item.source || '7110')}</strong></div><div class="kv"><span>登记时间</span><strong>${E(dateText(item.created_at))}</strong></div>
      </div></div>
      <div class="detail-section"><h3>空间范围</h3>${ext}<p class="detail-hint">${item.category === 'imagery' ? '此处显示相机中心点的 WGS84 包络，不是照片地面覆盖范围；绝对定位精度需结合控制点或独立测量核验。' : '空间查询接口应使用登记的 WGS84 范围。旧目录的 Range 不一定代表地面覆盖；缺少明确坐标系时不会绘制到地图上。'}</p></div>
      <div class="detail-section"><h3>资源访问 / 加载入口</h3><div class="api-box"><code>${item.access_url ? `${E(item.access_method || 'GET')} ${E(item.access_url)}` : '暂无已登记的直接入口'}</code>${item.access_url ? '<button class="api-copy" id="copy-api">复制</button>' : ''}</div><div class="detail-links" style="margin-top:9px">${actions.join('')}</div>
        ${item.rename_kind ? `<form id="rename-form" class="rename-form" hidden><label for="rename-name">资源名称</label><input id="rename-name" name="name" maxlength="200" required value="${E(item.name)}"><div class="rename-actions"><button class="button primary" type="submit">保存名称</button><button class="button ghost" id="rename-cancel" type="button">取消</button></div><p id="rename-error" role="alert"></p><small>只修改显示名称；资源 ID、文件路径和场景图层名称保持不变。${item.rename_kind === 'legacy' ? '旧镜像使用本地名称覆盖，不改原始镜像。' : ''}</small></form>` : ''}</div>
      ${item.delete_kind ? `<div class="detail-hint delete-note">${E(deleteNote)}</div>` : ''}
      ${item.description ? `<div class="detail-section"><h3>说明</h3><div class="detail-hint">${E(item.description)}</div></div>` : ''}
      <div class="detail-section"><h3>元数据</h3><pre class="json-box">${E(JSON.stringify(redact(raw || {}), null, 2))}</pre></div>`;
    $('detail-backdrop').hidden = false; $('detail-panel').classList.add('open'); $('detail-panel').setAttribute('aria-hidden', 'false');
    $('close-detail').focus();
    $('copy-api')?.addEventListener('click', async () => { await navigator.clipboard.writeText(new URL(item.access_url, location.origin).href); toast('接口地址已复制'); });
    $('rename-resource')?.addEventListener('click', () => {
      $('rename-form').hidden = false;
      $('rename-name').focus(); $('rename-name').select();
    });
    $('rename-cancel')?.addEventListener('click', () => { $('rename-form').hidden = true; $('rename-error').textContent = ''; });
    $('rename-form')?.addEventListener('submit', async (event) => {
      event.preventDefault();
      const name = $('rename-name').value.trim();
      const error = $('rename-error'); error.textContent = '';
      if (!name || name.length > 200 || /[\x00-\x1f\x7f]/.test(name)) { error.textContent = '请输入 1 至 200 个可显示字符'; return; }
      if (name === item.name) { $('rename-form').hidden = true; return; }
      const button = $('rename-form').querySelector('[type="submit"]'); button.disabled = true;
      try {
        await api(`/admin/resources/${item.rename_kind}/${encodeURIComponent(item.id)}`, { method: 'PATCH', body: JSON.stringify({ name }) });
        await loadCatalog();
        const updated = state.items.find(row => row.id === item.id && row.category === item.category);
        if (updated) openDetail(updated); else closeDetail();
        toast('资源名称已更新');
      } catch (err) { error.textContent = `重命名失败：${err.message}`; button.disabled = false; }
    });
    $('preview-endpoint')?.addEventListener('click', async (event) => {
      const button = event.currentTarget; button.disabled = true; button.textContent = '读取中…';
      try {
        const response = await api(item.access_url); const panel = $('detail-content');
        const preview = document.createElement('div'); preview.className = 'detail-section'; preview.innerHTML = '<h3>接口响应预览</h3>';
        const pre = document.createElement('pre'); pre.className = 'json-box';
        pre.textContent = JSON.stringify(response, null, 2).slice(0, 14000); preview.append(pre); panel.insertBefore(preview, panel.querySelector('.detail-section:last-child'));
        button.textContent = '已加载响应';
      } catch (err) { button.disabled = false; button.textContent = '重试读取'; toast(`读取失败：${err.message}`); }
    });
    $('delete-resource')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      const confirmText = item.delete_kind === 'subset'
        ? `确定删除区域子资源“${item.name}”及其场景关联吗？源瓦片不会删除。`
        : `确定永久删除“${item.name}”及其本地文件吗？此操作不可撤销。`;
      if (!window.confirm(confirmText)) return;
      button.disabled = true; button.textContent = '删除中…';
      try {
        const result = await api(`/admin/resources/${item.delete_kind}/${encodeURIComponent(item.id)}`, { method: 'DELETE' });
        closeDetail(); await loadCatalog();
        const cleanupPending = result.storage_cleanup_complete === false || result.scene_reference_cleanup_complete === false
          || result.derived_subsets_cleanup_complete === false;
        toast(cleanupPending ? '资源已删除，但部分磁盘/场景清理未完成，请检查服务日志' : `已删除；清理场景引用 ${result.scene_references_removed || 0} 项`);
      } catch (err) { button.disabled = false; button.textContent = '重试删除'; toast(`删除失败：${err.message}`); }
    });
  }
  function closeDetail() { $('detail-panel').classList.remove('open'); $('detail-panel').setAttribute('aria-hidden', 'true'); setTimeout(() => $('detail-backdrop').hidden = true, 220); }
  function showPage(page) {
    state.activePage = page; const catalog = page === 'catalog';
    $('catalog-page').hidden = !catalog; $('loading-page').hidden = catalog;
    $('page-title').textContent = catalog ? '资源目录' : '接入与加载'; $('page-crumb').textContent = catalog ? '资源目录' : '接入与加载';
    $('page-description').textContent = catalog ? '查看 7110 中已登记的数据、空间范围与访问方式。' : '使用经过预检的管理员上传入口；发布后回到资源目录核对数据与加载地址。';
    document.querySelectorAll('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.nav === page));
    if (!catalog) renderLoadingPage();
  }
  const ingest = [
    { id: 'vector', label: '矢量信息', format: '统一数据包 · SHP', description: '选择完整数据包文件夹或 ZIP；服务端校验同名组件、CRS、几何和位置审核信息。', route: '/admin/data-packages/uploads', store: 'PostGIS', icon: '⌁', enabled: true },
    { id: 'pointcloud', label: '点云与三维瓦片', format: '统一数据包 · PLY / 3D Tiles', description: '选择完整数据包文件夹或 ZIP；服务端校验空间范围、PLY 或完整瓦片引用后发布。', route: '/admin/data-packages/uploads', store: '点云版本库', icon: '⁙', enabled: true },
    { id: 'video', label: '视频资料组', format: '统一数据包 · MP4 + 标定成果', description: '选择完整数据包文件夹或 ZIP；服务端检查精标定、GeoTIFF CRS、相机位置和产品覆盖范围。', route: '/admin/data-packages/uploads', store: '视频组接口', icon: '▷', enabled: true },
    { id: 'raster', label: '栅格信息', format: '统一数据包 · TMS 期次瓦片', description: '选择完整数据包文件夹或 ZIP；服务端校验期次元数据、瓦片矩阵、CRS、范围和文件树。', route: '/admin/data-packages/uploads', store: '时序栅格', icon: '▦', enabled: true },
    { id: 'oblique', label: '倾斜影像', format: '统一数据包 · JPEG + 相机位姿', description: '选择完整数据包文件夹或 ZIP；服务端校验相机组、逐图位姿、坐标转换和原图。', route: '/admin/data-packages/uploads', store: '影像索引', icon: '▧', enabled: true },
    { id: 'other', label: '文本与扩展接口', format: '待定义数据契约', description: '暂未提供通用文件上传 API；需先确定元数据、范围和校验规则。', route: '/docs', store: '扩展接口', icon: '⌘', enabled: false }
  ];
  function renderLoadingPage() {
    $('ingest-grid').innerHTML = ingest.map(x => `<article class="ingest-card ${x.enabled ? 'is-enabled' : 'is-disabled'}" ${x.enabled ? `role="button" tabindex="0" data-ingest-type="${x.id}"` : ''}><div class="ingest-top"><i class="ingest-icon">${x.icon}</i><span class="supported ${x.enabled ? '' : 'unsupported'}">${x.enabled ? '统一上传' : '暂不接入'}</span></div><h3>${E(x.label)}</h3><div class="ingest-format">${E(x.format)}</div><p>${E(x.description)}</p><div class="ingest-route"><span>${E(x.store)}</span><a href="/docs" target="_blank" rel="noopener" title="查看接口文档" onclick="event.stopPropagation()">接口文档 ↗</a></div></article>`).join('');
    $('ingest-grid').querySelectorAll('[data-ingest-type]').forEach(card => {
      const kind = card.dataset.ingestType;
      card.onclick = () => openPackageUpload(kind);
      card.onkeydown = event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openPackageUpload(kind); } };
    });
    const routes = [
      ['统一数据包上传', 'POST /admin/data-packages/uploads  ·  PUT /uploads/{id}/files/{index}/chunks/{chunk}'],
      ['导入状态查询', 'GET /admin/data-packages/uploads/{id}  ·  POST /uploads/{id}/finalize'],
      ['资源目录与分页', 'POST /api/ArScene/GetSceneResourceByPage'], ['3D Tiles / Mesh 文件', 'GET /api/Mesh/GetMesh3DTilesFile/{id}/{path}'],
      ['点云 3D Tiles 文件', 'GET /api/PointCload/GetPointCloud3DTilesFile/{id}/{path}'], ['时序栅格瓦片', 'GET /spatial/raster-collections/{id}/epochs/{date}/tiles/{z}/{x}/{y}'],
      ['矢量范围查询', 'GET /spatial/datasets/{id}/features?bbox=minLon,minLat,maxLon,maxLat'], ['视频组目录与文件', 'GET /videos/groups/{id}  ·  /files/{relative_path}'],
      ['统一 POI 查询', 'GET /spatial/pois/summary  ·  POST /spatial/pois/query  ·  GET /spatial/pois/{poi_id}'],
      ['倾斜影像范围查询', 'GET /spatial/oblique-images?bbox=minLon,minLat,maxLon,maxLat'],
      ['管理员重命名资源', 'PATCH /admin/resources/{kind}/{uuid}'],
      ['管理员删除本地资源', 'DELETE /admin/resources/{kind}/{uuid}']
    ];
    $('route-list').innerHTML = routes.map(([label, route]) => `<div class="route-item"><strong>${E(label)}</strong><code>${E(route)}</code><button data-copy="${E(route)}">复制接口模板</button></div>`).join('');
    $('route-list').querySelectorAll('[data-copy]').forEach(b => b.onclick = async () => { await navigator.clipboard.writeText(b.dataset.copy); toast('接口模板已复制'); });
  }
  function uploadPanel(title, subtitle, form) {
    const panel = $('upload-workspace');
    panel.hidden = false;
    panel.innerHTML = `<div class="upload-head"><div><div class="eyebrow">STRICT UPLOAD</div><h2>${E(title)}</h2><p>${E(subtitle)}</p></div><button class="button ghost" type="button" id="close-upload">收起</button></div>${form}<div class="upload-status" id="upload-status" role="status" aria-live="polite" hidden><div class="upload-status-title" id="upload-status-title"></div><div class="upload-progress-track"><div class="upload-progress-bar" id="upload-progress-bar"></div></div><div class="upload-status-detail" id="upload-status-detail"></div></div>`;
    $('close-upload').onclick = () => { panel.hidden = true; panel.innerHTML = ''; };
    panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  function openPackageUpload(kind) {
    const item = ingest.find(x => x.id === kind); if (!item || !item.enabled) return;
    uploadPanel(`上传 ${item.label} 数据包`, '五类数据共用一个包结构、一个分块上传接口和一个导入状态查询接口。包根目录必须有 package.json 与 data/。',
      `<form id="package-upload-form" class="upload-form"><div class="upload-error" id="upload-error" role="alert"></div><div class="form-grid"><label class="wide">数据包文件夹<input id="package-folder" type="file" webkitdirectory directory multiple><small>选择包含 package.json 和 data/ 的完整文件夹。</small></label><label class="wide">或选择 ZIP 压缩包<input id="package-zip" type="file" accept=".zip,application/zip"><small>ZIP 根目录应包含 package.json 和 data/；允许 ZIP 外包一层文件夹。</small></label><div class="wide" id="package-summary" hidden></div></div><div class="upload-actions"><span class="upload-limit">统一上传接口 · 8 MiB 分块 · 支持重选同一文件夹续传</span><button class="button primary" type="submit">校验并上传</button><button class="button ghost" id="cancel-package-upload" type="button" hidden>取消未完成上传</button></div></form>`);
    const folder = $('package-folder'), zip = $('package-zip'), form = $('package-upload-form');
    folder.onchange = () => { if (folder.files.length) zip.value = ''; };
    zip.onchange = () => { if (zip.files.length) folder.value = ''; };
    form.onsubmit = event => uploadStandardPackage(event, kind);
    $('cancel-package-upload').onclick = async () => {
      const pending = readPendingUpload('package'); if (!pending) return;
      try { await api(`/admin/data-packages/uploads/${encodeURIComponent(pending.upload_id)}`, { method: 'DELETE' }); clearPendingUpload('package'); toast('已取消数据包上传'); }
      catch (error) { uploadError(error.message); }
    };
  }
  function packageSelection() {
    const archive = $('package-zip')?.files?.[0];
    if (archive) {
      if (!archive.name.toLowerCase().endsWith('.zip') || !archive.size) throw new Error('请选择非空 ZIP 压缩包');
      if (archive.size > 100 * 1024 ** 3) throw new Error('ZIP 压缩包超过 100 GiB');
      return { transport: 'zip', files: [{ file: archive, path: safeRelativePath(archive.name) }] };
    }
    const selected = fileListFromInput($('package-folder'));
    if (!selected.length) throw new Error('请选择完整数据包文件夹或 ZIP');
    if (selected.length > 10000) throw new Error('数据包文件数超过 10,000');
    const first = selected[0].webkitRelativePath || selected[0].name;
    const root = first.includes('/') ? first.split('/')[0] : '';
    const files = selected.map(file => {
      const full = file.webkitRelativePath || file.name;
      if (root && !full.startsWith(`${root}/`)) throw new Error('请一次只选择一个完整数据包文件夹');
      const rel = root ? full.slice(root.length + 1) : full;
      return { file, path: safeRelativePath(rel) };
    });
    const paths = new Set();
    for (const entry of files) {
      const key = entry.path.toLocaleLowerCase();
      if (paths.has(key)) throw new Error(`数据包中存在重复路径：${entry.path}`);
      paths.add(key);
      if (!entry.file.size) throw new Error(`文件为空：${entry.path}`);
      if (entry.file.size > 24 * 1024 ** 3) throw new Error(`单个文件超过 24 GiB：${entry.path}`);
    }
    if (!paths.has('package.json')) throw new Error('包根目录缺少 package.json');
    if (!files.some(entry => entry.path.startsWith('data/'))) throw new Error('数据包缺少 data/ 载荷目录');
    const manifestFile = files.find(entry => entry.path === 'package.json').file;
    return { transport: 'folder', files, manifestFile };
  }
  async function uploadStandardPackage(event, expectedType) {
    event.preventDefault(); const form = event.currentTarget; setFormBusy(form, true);
    try {
      $('upload-error').textContent = '';
      const selected = packageSelection();
      let manifest = null;
      if (selected.manifestFile) {
        try { manifest = JSON.parse(await selected.manifestFile.text()); }
        catch { throw new Error('package.json 不是有效 JSON'); }
        if (manifest.schema !== 'deqing.data-package.v1') throw new Error('package.json schema 必须为 deqing.data-package.v1');
        if (manifest.type !== expectedType) throw new Error(`当前选择的是 ${expectedType}，但包内 type 为 ${manifest.type || '空'}`);
        if (!manifest.location_review || manifest.location_review.confirmed !== true) throw new Error('package.json 必须包含已确认的 location_review');
      }
      const selectedFiles = selected.files;
      const total = selectedFiles.reduce((sum, entry) => sum + entry.file.size, 0);
      if (total > 100 * 1024 ** 3) throw new Error('数据包超过 100 GiB 总量上限');
      const hashed = await hashFiles(selectedFiles.map(entry => entry.file), file => selectedFiles.find(entry => entry.file === file)?.path || file.name);
      const hashes = new Map(hashed.map(entry => [entry.file, entry.sha256]));
      const descriptors = selectedFiles.map(entry => ({ path: entry.path, size_bytes: entry.file.size, sha256: hashes.get(entry.file) }));
      const fingerprint = JSON.stringify({ expectedType, transport: selected.transport, files: descriptors });
      let pending = readPendingUpload('package'), status, uploadId;
      if (pending) {
        if (pending.fingerprint !== fingerprint) throw new Error(`存在未完成的数据包上传 ${pending.upload_id}。请重新选择相同文件继续，或点击“取消未完成上传”。`);
        uploadId = pending.upload_id;
        status = await api(`/admin/data-packages/uploads/${encodeURIComponent(uploadId)}`);
      } else {
        const created = await api('/admin/data-packages/uploads', { method: 'POST', body: JSON.stringify({
          transport: selected.transport, expected_type: expectedType, files: descriptors
        }) });
        uploadId = created.upload_id;
        pending = { upload_id: uploadId, fingerprint }; savePendingUpload('package', pending);
        status = await api(`/admin/data-packages/uploads/${encodeURIComponent(uploadId)}`);
      }
      $('cancel-package-upload').hidden = false;
      if (status.status === 'complete') {
        clearPendingUpload('package');
        uploadMessage('数据包已入库', `${status.result?.kind || expectedType} · ID ${status.result?.id || '已登记'}`, 100);
        await loadCatalog(); return;
      }
      if (status.status === 'failed') throw new Error(status.error || '数据包导入失败；可取消草稿后修正数据包再上传');
      const byIndex = new Map(selectedFiles.map((entry, index) => [index, entry]));
      let sent = 0;
      for (const remote of status.files) {
        const entry = byIndex.get(remote.index); if (!entry) throw new Error('恢复上传的文件清单与当前选择不一致');
        const received = new Set(remote.received_chunks || []);
        const chunkSize = 8 * 1024 * 1024, chunks = Math.ceil(entry.file.size / chunkSize);
        for (let index = 0; index < chunks; index++) {
          const start = index * chunkSize, end = Math.min(entry.file.size, start + chunkSize);
          if (!received.has(index)) {
            await fetchRetry(`/admin/data-packages/uploads/${encodeURIComponent(uploadId)}/files/${remote.index}/chunks/${index}`, {
              method: 'PUT', headers: { Authorization: `Bearer ${token()}`, 'Content-Type': 'application/octet-stream',
                'Content-Range': `bytes ${start}-${end - 1}/${entry.file.size}` }, body: entry.file.slice(start, end)
            }, `${entry.path} 分块 ${index + 1}`);
          }
          sent += end - start; setUploadProgress(`上传 ${entry.path}`, sent, total);
        }
      }
      await api(`/admin/data-packages/uploads/${encodeURIComponent(uploadId)}/finalize`, { method: 'POST', body: '{}' });
      while (true) {
        status = await api(`/admin/data-packages/uploads/${encodeURIComponent(uploadId)}`);
        if (status.status === 'complete') break;
        if (status.status === 'failed') throw new Error(status.error || '数据包入库失败；可取消草稿后修正再上传');
        uploadMessage(status.status === 'importing' ? '服务端正在校验并入库' : '服务端正在整理数据包', `上传编号 ${uploadId}`, 100);
        await new Promise(resolve => setTimeout(resolve, 2000));
      }
      clearPendingUpload('package'); $('cancel-package-upload').hidden = true;
      uploadMessage('数据包已入库', `${status.result?.kind || expectedType} · ID ${status.result?.id || '已登记'}`, 100);
      toast('数据包已导入'); await loadCatalog();
    } catch (error) { uploadError(error.message || '数据包上传失败'); }
    finally { setFormBusy(form, false); }
  }
  function uploadMessage(title, detail = '', percent = null) {
    const box = $('upload-status'); if (!box) return;
    box.hidden = false; $('upload-status-title').textContent = title;
    $('upload-status-detail').textContent = detail;
    $('upload-progress-bar').style.width = percent === null ? '0%' : `${Math.max(0, Math.min(100, percent))}%`;
  }
  function uploadError(message) { const node = $('upload-error'); if (node) node.textContent = message; uploadMessage('上传未完成', message); }
  function setFormBusy(form, busy) { form.querySelectorAll('button,input,select,textarea').forEach(el => { if (el.type !== 'file') el.disabled = busy; else el.disabled = busy; }); }
  function fileListFromInput(input) { return Array.from(input?.files || []); }
  function setUploadProgress(label, done, total) {
    const percent = total > 0 ? done / total * 100 : 0;
    uploadMessage(label, `${bytes(done)} / ${bytes(total)} · ${percent.toFixed(1)}%`, percent);
  }
  function readPendingUpload(kind) {
    if (state.pendingUploads[kind]) return state.pendingUploads[kind];
    try { const raw = sessionStorage.getItem(`deqing7110_pending_${kind}`); if (raw) state.pendingUploads[kind] = JSON.parse(raw); } catch {}
    return state.pendingUploads[kind] || null;
  }
  function savePendingUpload(kind, value) {
    state.pendingUploads[kind] = value;
    try { sessionStorage.setItem(`deqing7110_pending_${kind}`, JSON.stringify(value)); } catch {}
  }
  function clearPendingUpload(kind) {
    delete state.pendingUploads[kind];
    try { sessionStorage.removeItem(`deqing7110_pending_${kind}`); } catch {}
  }
  function sha256File(file, label) {
    return new Promise((resolve, reject) => {
      const worker = new Worker('/admin/console/assets/sha256-worker.js?v=unified-packages-1');
      worker.onmessage = event => {
        const data = event.data || {};
        if (data.type === 'progress') uploadMessage(`计算校验值：${label}`, `${bytes(data.loaded)} / ${bytes(file.size)}`, file.size ? data.loaded / file.size * 100 : 0);
        else if (data.type === 'done') { worker.terminate(); resolve(data.sha256); }
        else if (data.type === 'error') { worker.terminate(); reject(new Error(data.message || 'SHA-256 计算失败')); }
      };
      worker.onerror = () => { worker.terminate(); reject(new Error('浏览器 SHA-256 工作线程失败')); };
      worker.postMessage({ file });
    });
  }
  async function hashFiles(files, labelFor) {
    const total = files.reduce((n, f) => n + f.size, 0); let done = 0;
    const result = [];
    for (const file of files) {
      const sha256 = await sha256File(file, labelFor(file));
      result.push({ file, sha256 }); done += file.size; setUploadProgress('文件校验完成', done, total);
    }
    return result;
  }
  async function fetchRetry(url, options, label) {
    let last;
    for (let attempt = 0; attempt < 4; attempt++) {
      try {
        const response = await fetch(url, { ...options, cache: 'no-store' });
        if (response.ok) return response;
        const text = await response.text();
        if (response.status === 401) { sessionStorage.removeItem(TOKEN_KEY); showLogin(); throw new Error('登录已过期，请重新登录后再上传'); }
        if (response.status < 500 && response.status !== 429) throw new Error(text || `${label} HTTP ${response.status}`);
        last = new Error(text || `${label} HTTP ${response.status}`);
      } catch (error) {
        if (error instanceof TypeError || /network|fetch/i.test(error.message)) last = error;
        else throw error;
      }
      if (attempt < 3) await new Promise(resolve => setTimeout(resolve, 500 * (2 ** attempt)));
    }
    throw last || new Error(`${label} failed`);
  }
  function safeRelativePath(value) {
    const path = String(value || '');
    if (!path || path.includes('\\') || path.includes('\0') || path.startsWith('/') || /^[A-Za-z]:/.test(path) || path.split('/').some(p => !p || p === '.' || p === '..' || /[\u0000-\u001f]/.test(p)) || new TextEncoder().encode(path).length > 1024) throw new Error(`不安全的文件路径：${value}`);
    return path;
  }
  async function handleLogin(event) {
    event.preventDefault(); const form = new FormData(event.currentTarget); const username = String(form.get('username') || '').trim(), password = String(form.get('password') || '');
    if (!window.md5) { $('login-error').textContent = '登录组件未加载，请刷新页面。'; return; }
    $('login-error').textContent = '';
    try {
      const result = await fetch('/api/Login/Login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ UserName: username, Password: window.md5(password) }) }).then(async r => { const body = await r.json(); if (!r.ok || body.State !== 0) throw new Error(body.Message || `HTTP ${r.status}`); return body; });
      const jwt = result.Data?.Token;
      if (!jwt) throw new Error('登录响应中没有访问令牌');
      sessionStorage.setItem(TOKEN_KEY, jwt);
      const user = await api('/api/User/GetByToken');
      if (String(user.Data?.Role || result.Data?.Role || '').toLowerCase() !== 'admin') { sessionStorage.removeItem(TOKEN_KEY); throw new Error('当前账号不是管理员，管理终端仅开放给管理员。'); }
      showApp(user.Data || result.Data); await loadCatalog();
    } catch (err) { $('login-error').textContent = err.message || '登录失败'; }
  }
  async function boot() {
    $('login-form').addEventListener('submit', handleLogin);
    $('refresh').onclick = () => loadCatalog().then(() => toast('目录已刷新')).catch(err => toast(err.message));
    $('search').addEventListener('input', e => { state.query = e.target.value; renderRows(); });
    $('category-filter').addEventListener('change', e => { state.selectedCategory = e.target.value; render(); });
    $('extent-filter').addEventListener('change', e => { state.extentFilter = e.target.value; renderRows(); });
    $('bbox-apply').onclick = () => {
      const parts = $('bbox-search').value.split(',').map(v => Number(v.trim()));
      if (parts.length !== 4 || !validBox(parts) || parts[0] >= parts[2] || parts[1] >= parts[3]) { toast('范围格式应为 minLon, minLat, maxLon, maxLat'); return; }
      state.bboxFilter = parts; renderRows();
    };
    $('bbox-search').addEventListener('keydown', e => { if (e.key === 'Enter') $('bbox-apply').click(); });
    $('bbox-clear').onclick = () => { state.bboxFilter = null; $('bbox-search').value = ''; renderRows(); };
    $('view-toggle').onclick = () => { state.cardView = !state.cardView; $('table-view').hidden = state.cardView; $('card-view').hidden = !state.cardView; $('view-toggle').textContent = state.cardView ? '▦' : '▤'; };
    $('go-loading').onclick = () => showPage('loading'); $('clear-map-filter').onclick = () => { state.query = ''; state.selectedCategory = 'all'; state.extentFilter = 'all'; state.bboxFilter = null; $('search').value = ''; $('bbox-search').value = ''; $('category-filter').value = 'all'; $('extent-filter').value = 'all'; render(); };
    document.querySelectorAll('.nav-item').forEach(b => b.onclick = () => showPage(b.dataset.nav));
    $('close-detail').onclick = closeDetail; $('detail-backdrop').onclick = closeDetail;
    $('logout').onclick = async () => { try { await api('/api/Login/Logout', { method: 'POST' }); } catch {} sessionStorage.removeItem(TOKEN_KEY); state.items = []; showLogin(); };
    document.addEventListener('keydown', e => { if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); $('search').focus(); } if (e.key === 'Escape') closeDetail(); });
    if (!token()) { showLogin(); return; }
    try {
      const user = await api('/api/User/GetByToken');
      if (String(user.Data?.Role || '').toLowerCase() !== 'admin') throw new Error('管理员权限已失效，请使用管理员账号重新登录。');
      showApp(user.Data); await loadCatalog();
    } catch (err) { sessionStorage.removeItem(TOKEN_KEY); showLogin(err.message); }
  }
  boot().catch(err => showLogin(err.message));
})();
