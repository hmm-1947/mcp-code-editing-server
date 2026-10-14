Bundled third-party code

graphify/        Graphify 0.9.90, unmodified copy of the `graphify` package from
                 https://github.com/Graphify-Labs/graphify (Apache-2.0, with MIT portions).
                 License texts: LICENSE, LICENSE-MIT, NOTICE (also shipped in licenses/graphify).
assets/vis-network.min.js   vis-network 9.1.6 standalone (Apache-2.0 / MIT), SRI hash matches graphify's pin.
assets/d3.v7.min.js         D3 v7 (ISC), used by the tree view.

Cody runs graphify through cody_cli/graph_worker.py. The assets replace the CDN
script tags so graph pages work offline.
To upgrade: replace the graphify/ folder, re-run the graph tests.
