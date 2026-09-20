# profitdog-ui

The browser half of [profitdog](../README.md): React + TypeScript + Vite, with
shadcn/ui components.

It holds no domain logic. Every figure it draws — kit costs, break-even times,
correlations — arrives already derived from `profitdog.server`, so there is only
ever one implementation of the rules. See the root README for why.

```bash
pnpm install
pnpm dev      # dev server, expects the API on http://127.0.0.1:5174
pnpm build    # writes dist/, which the server serves
```
