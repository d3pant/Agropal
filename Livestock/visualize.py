import json
from pathlib import Path

LIVESTOCK_DIR = Path(__file__).parent

with open(LIVESTOCK_DIR / "farm_profile.json") as f:
    farm = json.load(f)

with open(LIVESTOCK_DIR / "livestock_status.json") as f:
    status = json.load(f)

with open(LIVESTOCK_DIR / "neighboring_farms.json") as f:
    neighbors_data = json.load(f)

with open(LIVESTOCK_DIR / "wake_up_packet.json") as f:
    wake_up = json.load(f)

farm_centroid = farm["centroid"]
map_center = [farm_centroid["lat"], farm_centroid["lon"]]

color_map = {
    "cattle": "#DC2626",
    "horse": "#EA580C",
    "sheep": "#2563EB",
    "pig": "#9333EA",
    "goat": "#16A34A"
}

pens_data = []
for pen in farm["pens"]:
    status_pen = next((p for p in status["pens"] if p["pen_id"] == pen["pen_id"]), None)
    if status_pen:
        pens_data.append({
            "pen_id": pen["pen_id"],
            "name": pen["name"],
            "species": pen["species"],
            "count": pen["count"],
            "lat": pen["centroid"]["lat"],
            "lon": pen["centroid"]["lon"],
            "priority": status_pen.get("priority_score", 0),
            "decision": status_pen["decision"],
            "duration_hours": status_pen["route_duration_hours"],
            "distance_km": status_pen["assigned_evac_site"]["distance_km"] if status_pen["assigned_evac_site"] else 0,
            "evac_site": status_pen.get("assigned_evac_site") or status_pen.get("candidate_evac_site"),
            "geometry": status_pen.get("route_geometry"),
            "assessment_status": status_pen.get("assessment_status"),
            "duration_hours": status_pen.get("route_duration_hours"),
            "distance_km": status_pen.get("assigned_evac_site", {}).get("distance_km") if status_pen.get("assigned_evac_site") else None,
            "value": pen["count"] * pen["avg_market_value_usd"],
            "color": color_map.get(pen["species"], "#999")
        })

pens_data.sort(key=lambda p: p["priority"], reverse=True)

pens_json = json.dumps(pens_data)
pools_json = json.dumps(status.get("transport_pool", []))
farm_name = farm["farm_name"]
total_animals = sum(p["count"] for p in farm["pens"])
farm_lat = farm_centroid["lat"]
farm_lon = farm_centroid["lon"]
total_value = sum(p["value"] for p in pens_data)

html_content = """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Livestock Evacuation</title>

    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" />

    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Inter', sans-serif; background: #0f172a; color: #e2e8f0; height: 100vh; overflow: hidden; }
        .container { display: grid; grid-template-columns: 1fr 350px; height: 100vh; }
        #map { width: 100%; height: 100%; }
        .sidebar { background: #1e293b; border-left: 2px solid #3b82f6; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 20px; }
        .sidebar::-webkit-scrollbar { width: 6px; }
        .sidebar::-webkit-scrollbar-thumb { background: #3b82f6; border-radius: 3px; }

        h1 { font-size: 18px; font-weight: 700; margin-bottom: 10px; }
        .status { background: rgba(239, 68, 68, 0.1); border-left: 3px solid #ef4444; padding: 12px; border-radius: 6px; font-size: 12px; }
        .status strong { color: #fca5a5; }

        .stats { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
        .stat-box { background: rgba(59, 130, 246, 0.1); padding: 12px; border-radius: 6px; }
        .stat-value { font-size: 18px; font-weight: 700; color: #60a5fa; }
        .stat-label { font-size: 10px; color: #94a3b8; margin-top: 4px; text-transform: uppercase; }

        .section-title { font-size: 13px; font-weight: 700; color: #cbd5e1; margin-top: 15px; margin-bottom: 10px; border-bottom: 1px solid rgba(59, 130, 246, 0.3); padding-bottom: 8px; }

        .pen-list { display: flex; flex-direction: column; gap: 10px; }
        .pen-item { background: rgba(71, 85, 105, 0.3); border: 1px solid rgba(59, 130, 246, 0.4); border-radius: 6px; padding: 10px; cursor: pointer; transition: all 0.2s; border-left: 3px solid #cbd5e1; }
        .pen-item:hover { background: rgba(71, 85, 105, 0.5); border-color: #60a5fa; }
        .pen-item.active { background: rgba(59, 130, 246, 0.3); border-color: #60a5fa; box-shadow: 0 0 10px rgba(59, 130, 246, 0.3); }

        .pen-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
        .pen-id { font-weight: 700; color: #f1f5f9; font-size: 13px; }
        .pen-priority { background: rgba(59, 130, 246, 0.4); padding: 2px 6px; border-radius: 3px; font-size: 10px; color: #60a5fa; }
        .pen-species { font-size: 11px; color: #cbd5e1; margin-bottom: 4px; }
        .pen-value { font-size: 11px; color: #cbd5e1; margin-bottom: 6px; }

        .decision { display: inline-block; padding: 4px 8px; border-radius: 3px; font-size: 10px; font-weight: 600; }
        .decision.evacuate { background: rgba(34, 197, 94, 0.3); color: #86efac; }
        .decision.shelter { background: rgba(251, 146, 60, 0.3); color: #fdba74; }
        .decision.cannot { background: rgba(239, 68, 68, 0.3); color: #fca5a5; }

        .pool-item { background: rgba(139, 92, 246, 0.1); border: 1px solid rgba(139, 92, 246, 0.4); border-radius: 6px; padding: 10px; font-size: 11px; }
        .pool-stat { display: flex; justify-content: space-between; margin: 4px 0; color: #cbd5e1; }
        .pool-value { color: #c4b5fd; font-weight: 600; }

        .map-box { position: absolute; top: 15px; left: 15px; background: rgba(15, 23, 42, 0.9); border: 1px solid rgba(59, 130, 246, 0.4); border-radius: 8px; padding: 12px; color: #e2e8f0; font-size: 11px; max-width: 200px; z-index: 999; }
        .legend { position: absolute; bottom: 15px; left: 15px; background: rgba(15, 23, 42, 0.9); border: 1px solid rgba(59, 130, 246, 0.4); border-radius: 8px; padding: 12px; color: #e2e8f0; font-size: 10px; z-index: 999; }
    </style>
</head>
<body>
    <div class="container">
        <div id="map">
            <div class="map-box">
                <div style="color: #fca5a5; font-weight: 700; margin-bottom: 8px;">🔥 THREAT</div>
                <div><strong>Palisades Fire</strong><br>75 km away</div>
            </div>
            <div class="legend">
                <div style="font-weight: 700; margin-bottom: 8px;">Legend</div>
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; font-size: 9px;">
                    <div>🐄 Cattle</div>
                    <div>🐴 Horse</div>
                    <div>🐑 Sheep</div>
                    <div>🐷 Pig</div>
                    <div>🐐 Goat</div>
                    <div>✓ Evac Site</div>
                </div>
            </div>
        </div>

        <div class="sidebar">
            <div>
                <h1>🚜 Livestock Ops</h1>
                <div class="status">
                    <strong>CRITICAL THREAT</strong><br>
                    11 hours to impact
                </div>
            </div>

            <div class="stats">
                <div class="stat-box">
                    <div class="stat-value">$""" + f"{total_value:,.0f}" + """</div>
                    <div class="stat-label">Total Value</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value">""" + str(total_animals) + """</div>
                    <div class="stat-label">Animals</div>
                </div>
            </div>

            <div>
                <div class="section-title">📍 Top Routes</div>
                <div class="pen-list" id="penList"></div>
            </div>

            <div>
                <div class="section-title">💡 Cost Optimization</div>
                <div id="optimizationBox"></div>
            </div>

            <div>
                <div class="section-title">🤝 Partnerships</div>
                <div id="poolList"></div>
            </div>
        </div>
    </div>

    <script>
        const map = L.map('map').setView([""" + str(map_center[0]) + """, """ + str(map_center[1]) + """], 10);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { attribution: '© OSM', maxZoom: 19 }).addTo(map);

        const pens = """ + pens_json + """;
        const drawnRoutes = {};

        const farmIcon = L.divIcon({
            html: `<div style="display: flex; align-items: center; justify-content: center; width: 60px; height: 60px; background: linear-gradient(135deg, #fcd34d 0%, #f59e0b 100%); border: 3px solid #b45309; border-radius: 8px; box-shadow: 0 8px 20px rgba(0, 0, 0, 0.3); font-size: 28px;">🏪</div>`,
            className: '',
            iconSize: [60, 60],
            popupAnchor: [0, -30]
        });

        L.marker([""" + str(farm_lat) + """, """ + str(farm_lon) + """], { icon: farmIcon }).addTo(map).bindPopup(`<strong>""" + farm_name + """</strong><br>""" + str(total_animals) + """ animals`);

        async function drawRoute(pen) {
            if (!pen.evac_site) return;
            try {
                if (pen.geometry && pen.geometry.type === 'LineString') {
                    const coords = pen.geometry.coordinates.map(c => [c[1], c[0]]);

                    const shadowPolyline = L.polyline(coords, { color: '#000000', weight: 10, opacity: 0.2, dashArray: '6, 4', lineCap: 'round' }).addTo(map);

                    const polyline = L.polyline(coords, { color: pen.color, weight: 5, opacity: 0.85, dashArray: '8, 3', lineCap: 'round' }).addTo(map);

                    const distance = Number(pen.distance_km || 0).toFixed(1);
                    const duration = Number(pen.duration_hours || 0).toFixed(2);

                    polyline.bindPopup(`<div style="font-size: 11px; color: #1e293b;"><strong>${pen.pen_id} — ${pen.name}</strong><br>${pen.count}x ${pen.species} | $${(pen.value).toLocaleString()}<br><strong>${pen.evac_site.name} (candidate)</strong><br>${distance}km • ${duration}h<br>${pen.assessment_status || 'unknown'} smoke assessment</div>`, { minWidth: 220 });

                    drawnRoutes[pen.pen_id] = { polyline, shadow: shadowPolyline };
                }
            } catch (error) { console.error(error); }
        }

        pens.forEach(pen => {
            const penIcon = L.divIcon({
                html: `<div style="display: flex; align-items: center; justify-content: center; width: 40px; height: 40px; background: ${pen.color}; border: 2px solid ${pen.decision === 'evacuate' ? '#10b981' : '#ef4444'}; border-radius: 8px; box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2); font-size: 18px; color: white; font-weight: 700;">${pen.species[0].toUpperCase()}</div>`,
                className: '',
                iconSize: [40, 40],
                popupAnchor: [0, -20]
            });
            L.marker([pen.lat, pen.lon], { icon: penIcon }).addTo(map);
            drawRoute(pen);
        });

        const sites = {};
        pens.forEach(pen => {
            if (pen.evac_site && !sites[pen.evac_site.name]) {
                sites[pen.evac_site.name] = true;
                const siteIcon = L.divIcon({
                    html: `<div style="position: relative;"><div style="position: absolute; width: 100px; height: 100px; background: radial-gradient(circle, rgba(16, 185, 129, 0.3) 0%, transparent 70%); border-radius: 50%; top: -40px; left: -40px;"></div><div style="display: flex; align-items: center; justify-content: center; width: 80px; height: 80px; background: linear-gradient(135deg, #10b981 0%, #059669 100%); border: 4px solid #047857; border-radius: 12px; font-size: 40px; box-shadow: 0 8px 25px rgba(16, 185, 129, 0.4); z-index: 10;">✓</div></div>`,
                    className: '',
                    iconSize: [80, 80],
                    popupAnchor: [0, -40]
                });
                L.marker([pen.evac_site.lat, pen.evac_site.lon], { icon: siteIcon }).addTo(map);
            }
        });

        const fireIcon = L.divIcon({
            html: `<div style="display: flex; align-items: center; justify-content: center; width: 50px; height: 50px; background: rgba(239, 68, 68, 0.2); border: 2px solid #ef4444; border-radius: 8px; font-size: 24px; animation: pulse 2s infinite;">🔥</div>`,
            className: '',
            iconSize: [50, 50],
            popupAnchor: [0, -25]
        });
        L.marker([33.72, -117.66], { icon: fireIcon }).addTo(map);

        const penList = document.getElementById('penList');
        const speciesEmoji = {'cattle': '🐄', 'horse': '🐴', 'sheep': '🐑', 'pig': '🐷', 'goat': '🐐'};

        pens.slice(0, 4).forEach((pen, idx) => {
            const penItem = document.createElement('div');
            penItem.className = 'pen-item';
            const decisionClass = pen.decision === 'evacuate' ? 'evacuate' : pen.decision === 'shelter_in_place' ? 'shelter' : 'cannot';
            penItem.innerHTML = `
                <div class="pen-row">
                    <span class="pen-id">${pen.pen_id} ${speciesEmoji[pen.species]}</span>
                    <span class="pen-priority">${pen.priority.toFixed(0)}</span>
                </div>
                <div class="pen-species">${pen.count}x • $${(pen.value/1000).toFixed(0)}k</div>
                <span class="decision ${decisionClass}">${pen.decision === 'evacuate' ? '✓ GO' : '⚠️ HOLD'}</span>
            `;
            penItem.addEventListener('click', () => {
                document.querySelectorAll('.pen-item').forEach(item => item.classList.remove('active'));
                penItem.classList.add('active');
                map.setView([pen.lat, pen.lon], 12);
                Object.values(drawnRoutes).forEach(route => {
                    route.polyline.setStyle({ opacity: 0.15, weight: 3 });
                    route.shadow.setStyle({ opacity: 0.05 });
                });
                if (drawnRoutes[pen.pen_id]) {
                    drawnRoutes[pen.pen_id].polyline.setStyle({ opacity: 0.95, weight: 6 });
                    drawnRoutes[pen.pen_id].shadow.setStyle({ opacity: 0.3 });
                    drawnRoutes[pen.pen_id].polyline.openPopup();
                }
            });
            penList.appendChild(penItem);
        });

        // Cost optimization
        const statusData = """ + json.dumps(status) + """;
        const optimization = statusData.evacuation_optimization || {};
        if (optimization.summary) {
            const optBox = document.getElementById('optimizationBox');
            const summary = optimization.summary;
            optBox.innerHTML = `
                <div class="pool-item">
                    <div style="font-weight: 600; color: #86efac; margin-bottom: 6px;">Transport Capacity</div>
                    <div class="pool-stat">
                        <span>Can Save:</span>
                        <span class="pool-value">$${summary.value_can_save_usd.toLocaleString()}</span>
                    </div>
                    <div class="pool-stat">
                        <span>At Risk:</span>
                        <span class="pool-value" style="color: #fca5a5;">$${summary.potential_loss_usd.toLocaleString()}</span>
                    </div>
                    <div class="pool-stat">
                        <span>Loss Rate:</span>
                        <span class="pool-value" style="color: #fca5a5;">${summary.loss_percentage}%</span>
                    </div>
                </div>
            `;
        }

        const poolList = document.getElementById('poolList');
        const pools = """ + pools_json + """;
        pools.slice(0, 2).forEach((pool, idx) => {
            const poolDiv = document.createElement('div');
            poolDiv.className = 'pool-item';
            poolDiv.innerHTML = `
                <div style="font-weight: 600; color: #c4b5fd; margin-bottom: 6px;">Partnership ${idx + 1}</div>
                <div class="pool-stat">
                    <span>Save:</span>
                    <span class="pool-value">${pool.time_saved_minutes.toFixed(0)}min</span>
                </div>
                <div class="pool-stat">
                    <span>Cost:</span>
                    <span class="pool-value">$${pool.estimated_cost_sharing_usd.toLocaleString()}</span>
                </div>
            `;
            poolList.appendChild(poolDiv);
        });
    </script>
</body>
</html>
"""

with open(LIVESTOCK_DIR / "evacuation_dashboard.html", "w") as f:
    f.write(html_content)

print("✅ Simplified, clean dashboard created!")
print("📊 Shows: Top 4 pens, 2 partnerships, clear decision status")
print("💰 Using USDA NASS livestock prices")
