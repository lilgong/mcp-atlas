'use strict';

// Google Maps MCP 0.6.2 keeps its tool declarations and result handlers. Only
// its private fetch binding is replaced; no global fetch/module cache mutation.
const { readFileSync } = require('node:fs');
const { createRequire } = require('node:module');
const { pathToFileURL } = require('node:url');

const MODES = { driving: 'DRIVE', walking: 'WALK', bicycling: 'BICYCLE', transit: 'TRANSIT' };
const LOCATION = (p) => p && ({ lat: p.latitude, lng: p.longitude });
const distance = (meters = 0, text) => ({ value: meters,
  text: text || (meters < 1000 ? `${meters} m` : `${Number((meters / 1000).toFixed(1))} km`) });
function duration(raw = '0s', text) {
  if (!/^\d+(?:\.\d+)?s$/.test(raw)) throw new Error('Invalid Google Routes duration');
  const seconds = Math.round(Number(raw.slice(0, -1)));
  return { value: seconds, text: text || `${Math.max(1, Math.round(seconds / 60))} mins` };
}
function waypoint(value) {
  if (value.startsWith('place_id:')) return { placeId: value.slice(9) };
  const match = value.match(/^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$/);
  return match ? { location: { latLng: { latitude: Number(match[1]), longitude: Number(match[2]) } } }
    : { address: value };
}
function place(p) {
  return { name: p.displayName?.text, formatted_address: p.formattedAddress,
    geometry: { location: LOCATION(p.location) }, place_id: p.id, rating: p.rating, types: p.types };
}
function openingHours(p) {
  const hours = p.regularOpeningHours;
  if (!hours && !p.currentOpeningHours) return undefined;
  const point = (v) => v && ({ day: v.day,
    time: `${String(v.hour ?? 0).padStart(2, '0')}${String(v.minute ?? 0).padStart(2, '0')}` });
  return { open_now: p.currentOpeningHours?.openNow ?? hours?.openNow,
    periods: hours?.periods?.map((v) => ({ open: point(v.open), close: point(v.close) })),
    weekday_text: hours?.weekdayDescriptions };
}
function legacyError(data, status) {
  const code = data.error?.code || status;
  return { status: code === 429 ? 'OVER_QUERY_LIMIT' : code === 400 ? 'INVALID_REQUEST'
    : code === 404 ? 'NOT_FOUND' : code === 401 || code === 403 ? 'REQUEST_DENIED' : 'UNKNOWN_ERROR',
    error_message: `HTTP ${code}: ${data.error?.message || 'Google Maps request failed'}` };
}

function createLegacyFetch(fetchImpl = globalThis.fetch) {
  return async function legacyFetch(input, options) {
    const url = new URL(String(input));
    const endpoints = ['/maps/api/place/textsearch/json', '/maps/api/place/details/json',
      '/maps/api/directions/json', '/maps/api/distancematrix/json'];
    if (url.hostname !== 'maps.googleapis.com' || !endpoints.includes(url.pathname)) {
      return fetchImpl(input, options);
    }
    const params = url.searchParams;
    const key = params.get('key');
    async function request(endpoint, body, fields) {
      const response = await fetchImpl(endpoint, { method: body ? 'POST' : 'GET',
        headers: { 'X-Goog-Api-Key': key, 'X-Goog-FieldMask': fields, 'Content-Type': 'application/json' },
        ...(body ? { body: JSON.stringify(body) } : {}) });
      const data = await response.json();
      if (!response.ok || data.error) return { failure: legacyError(data, response.status) };
      return { data };
    }
    let result;
    if (url.pathname === endpoints[0]) {
      const body = { textQuery: params.get('query') };
      if (params.has('location')) {
        const [latitude, longitude] = params.get('location').split(',').map(Number);
        body.locationBias = { circle: { center: { latitude, longitude }, radius: Number(params.get('radius') || 50000) } };
      }
      const r = await request('https://places.googleapis.com/v1/places:searchText', body,
        'places.id,places.displayName,places.formattedAddress,places.location,places.rating,places.types');
      result = r.failure || { status: 'OK', results: (r.data.places || []).map(place) };
    } else if (url.pathname === endpoints[1]) {
      const id = params.get('place_id').replace(/^places\//, '');
      const r = await request(`https://places.googleapis.com/v1/places/${encodeURIComponent(id)}`, null,
        'id,displayName,formattedAddress,location,nationalPhoneNumber,websiteUri,rating,reviews,regularOpeningHours,currentOpeningHours');
      const p = r.data;
      result = r.failure || { status: 'OK', result: { ...place(p),
        formatted_phone_number: p.nationalPhoneNumber, website: p.websiteUri, opening_hours: openingHours(p),
        reviews: p.reviews?.map((v) => ({ author_name: v.authorAttribution?.displayName,
          author_url: v.authorAttribution?.uri, profile_photo_url: v.authorAttribution?.photoUri,
          rating: v.rating, text: v.text?.text, language: v.text?.languageCode,
          original_language: v.originalText?.languageCode,
          relative_time_description: v.relativePublishTimeDescription,
          time: v.publishTime ? Math.floor(Date.parse(v.publishTime) / 1000) : undefined })) } };
    } else {
      const mode = MODES[params.get('mode') || 'driving'];
      if (!mode) return { json: async () => ({ status: 'INVALID_REQUEST', error_message: 'Unsupported travel mode' }) };
      if (url.pathname === endpoints[2]) {
        const r = await request('https://routes.googleapis.com/directions/v2:computeRoutes',
          { origin: waypoint(params.get('origin')), destination: waypoint(params.get('destination')), travelMode: mode },
          'routes.description,routes.distanceMeters,routes.duration,routes.localizedValues,routes.legs.distanceMeters,routes.legs.duration,routes.legs.localizedValues,routes.legs.steps.distanceMeters,routes.legs.steps.staticDuration,routes.legs.steps.navigationInstruction,routes.legs.steps.travelMode,routes.legs.steps.localizedValues');
        result = r.failure || { status: 'OK', routes: (r.data.routes || []).map((v) => ({ summary: v.description || '',
          legs: (v.legs || []).map((leg) => ({ distance: distance(leg.distanceMeters, leg.localizedValues?.distance?.text),
            duration: duration(leg.duration, leg.localizedValues?.duration?.text),
            steps: (leg.steps || []).map((s) => ({ html_instructions: s.navigationInstruction?.instructions || '',
              distance: distance(s.distanceMeters, s.localizedValues?.distance?.text),
              duration: duration(s.staticDuration, s.localizedValues?.staticDuration?.text),
              travel_mode: ({ DRIVE: 'DRIVING', WALK: 'WALKING', BICYCLE: 'BICYCLING', TRANSIT: 'TRANSIT' })[s.travelMode] || s.travelMode })) })) })) };
      } else {
        const origins = params.get('origins').split('|');
        const destinations = params.get('destinations').split('|');
        const r = await request('https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix',
          { origins: origins.map((v) => ({ waypoint: waypoint(v) })), destinations: destinations.map((v) => ({ waypoint: waypoint(v) })), travelMode: mode },
          'originIndex,destinationIndex,status,condition,distanceMeters,duration,localizedValues');
        if (r.failure) result = r.failure;
        else {
          const rows = origins.map(() => ({ elements: destinations.map(() => ({ status: 'UNKNOWN_ERROR' })) }));
          for (const v of r.data) {
            // Proto JSON omits index=0 and successful status.code=0.
            const i = v.originIndex ?? 0, j = v.destinationIndex ?? 0;
            if (!rows[i]?.elements[j]) throw new Error('Invalid Google Routes matrix index');
            rows[i].elements[j] = v.status?.code
              ? { status: ({ 3: 'INVALID_REQUEST', 5: 'NOT_FOUND', 7: 'REQUEST_DENIED', 8: 'OVER_QUERY_LIMIT' })[v.status.code] || 'UNKNOWN_ERROR' }
              : v.condition !== 'ROUTE_EXISTS' ? { status: 'ZERO_RESULTS' }
              : { status: 'OK', distance: distance(v.distanceMeters, v.localizedValues?.distance?.text),
                duration: duration(v.duration, v.localizedValues?.duration?.text) };
          }
          // Routes matrix has no normalized-address fields. Keep the submitted
          // labels, rather than introducing extra billable geocoding requests.
          result = { status: 'OK', origin_addresses: origins, destination_addresses: destinations, rows };
        }
      }
    }
    return { json: async () => result };
  };
}

async function runGoogleMaps(entrypoint) {
  const resolveImport = createRequire(entrypoint);
  let fetchImports = 0;
  const source = readFileSync(entrypoint, 'utf8').replace(/^#![^\n]*\n/, '')
    .replace(/from\s+(["'])([^"']+)\1/g, (match, quote, name) => {
      if (name === 'node-fetch') {
        fetchImports++;
        return `from ${JSON.stringify(pathToFileURL(__filename).href)}`;
      }
      return `from ${JSON.stringify(pathToFileURL(resolveImport.resolve(name)).href)}`;
    });
  if (fetchImports !== 1) throw new Error('Unexpected Google Maps MCP source; refusing to change its tool contract');
  await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
}
module.exports = createLegacyFetch();
module.exports.createLegacyFetch = createLegacyFetch;
module.exports.runGoogleMaps = runGoogleMaps;
