'use strict';

// Preserve the pinned MCP contract while using Met's paginated search API.
async function installMetCompatibility(packageRoot) {
  const { pathToFileURL } = require('node:url');
  const { join } = require('node:path');
  const load = (file) => import(pathToFileURL(join(packageRoot, 'dist', file)).href);
  const { SearchMuseumObjectsTool, SearchInputSchema } = await load('tools/SearchMuseumObjectsTool.js');
  const { MetMuseumApiError } = await load('api/MetMuseumApiClient.js');
  if (SearchMuseumObjectsTool.prototype.execute._metPaginationCompat) return;
  async function execute(input) {
    try {
      const parsed = SearchInputSchema.safeParse(input);
      if (!parsed.success) {
        const message = parsed.error.issues.map((issue) =>
          `${issue.path.length ? issue.path.join('.') + ': ' : ''}${issue.message}`).join(' ');
        return { content: [{ type: 'text', text: message || 'Invalid search input.' }], isError: true };
      }
      const { page, pageSize, ...filters } = parsed.data;
      const url = new URL('https://collectionapi.metmuseum.org/public/collection/v1.1/search');
      for (const [key, value] of Object.entries(filters)) {
        if (value !== undefined && value !== false) url.searchParams.set(key, String(value));
      }
      url.searchParams.set('limit', String(pageSize));
      url.searchParams.set('offset', String((page - 1) * pageSize));
      const fetchPage = async () => {
        const result = await this.apiClient.fetchJson(url.toString());
        if (!Number.isInteger(result.total) || result.total < 0 ||
            (result.objectIDs != null && (!Array.isArray(result.objectIDs) ||
              !result.objectIDs.every(Number.isInteger))) ||
            (result.total > 0 && result.objectIDs == null)) {
          throw new MetMuseumApiError('The Met Museum API returned an unexpected search response. Please try again later.', undefined, true);
        }
        return result;
      };
      let result = await fetchPage();
      if (result.total === 0) {
        return { content: [{ type: 'text', text: 'No objects found' }],
          structuredContent: { total: 0, page: 1, pageSize, totalPages: 0, objectIDs: [] }, isError: false };
      }
      const totalPages = Math.max(1, Math.ceil(result.total / pageSize));
      const safePage = Math.min(page, totalPages);
      if (safePage !== page) {
        url.searchParams.set('offset', String((safePage - 1) * pageSize));
        result = await fetchPage();
      }
      const structuredContent = { total: result.total, page: safePage, pageSize,
        totalPages: Math.ceil(result.total / pageSize), objectIDs: result.objectIDs || [] };
      return { content: [{ type: 'text', text: `Total objects found: ${result.total}\nPage: ${safePage}/${structuredContent.totalPages}\nObject IDs: ${structuredContent.objectIDs.join(', ')}` }],
        structuredContent, isError: false };
    } catch (error) {
      const message = error instanceof MetMuseumApiError && error.isUserFriendly
        ? error.message : `Error searching museum objects: ${error}`;
      return { content: [{ type: 'text', text: message }], isError: true };
    }
  }
  execute._metPaginationCompat = true;
  SearchMuseumObjectsTool.prototype.execute = execute;
}
module.exports = { installMetCompatibility };
