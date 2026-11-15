# FireA-v1: model card

**English** · [Türkçe](MODEL_CARD.tr.md)

## What it does

FireA was trained to answer one question: if Aqua-MODIS had looked at a fire seen by VIIRS at the same moment, how many detections would it have reported?

The input is a cluster of nearby pixels from one VIIRS overpass. The output is not a single number but a probability distribution (negative binomial). From it we compute the expected number of MODIS detections, an 80% interval and the probability that MODIS detects the cluster at all.

## Model

- A set transformer with 2,122,178 parameters: 4 layers, d=192, 6 heads.
- Each cluster is given as up to 64 pixels (12 features per pixel), plus 25 context features describing the whole cluster.
- The loss is the negative binomial log-likelihood. It was trained with AdamW for 5 epochs on a CPU.
- Trained on: 2026-11-14T21:44:30Z.

## Data

- NASA FIRMS archive products: MODIS C6.1, VIIRS S-NPP and VIIRS NOAA-20 (375 m), 2019–2024.
- 15 regions. We took 50 days from each region: 26 from the fire season and 24 spread over all 12 months, so the model also sees winter and quiet periods.
- Volcanoes, static heat sources and offshore detections (`type` 1–3) were removed.
- Label: the number of MODIS detections that fall on the cluster in the Aqua overpass closest in time (at most 60 min apart).
- To make sure MODIS actually looked at the area, we required other Aqua detections 10–1,000 km away in the same overpass. Detections on the cluster itself were not counted as evidence; otherwise the label would decide whether a cluster enters the data. Clusters without evidence were not used.
- Context features include cluster size, FRP, brightness, confidence class, viewing geometry, local time, season (hemisphere-aware), absolute latitude and the number of VIIRS pixels within 25 km in the same overpass.
- At most 50,000 clusters per region. Train 283,341, validation 55,423, test 61,064 clusters.

| Region | Biome | Used for |
|---|---|---|
| Eastern Mediterranean (Türkiye, Greece) | Mediterranean shrubland/forest | test |
| Iberian Peninsula | Mediterranean shrubland/forest | train |
| California | Mediterranean shrubland/forest | train |
| Canadian boreal | Boreal forest | train |
| Siberia | Boreal forest | test |
| Amazon arc | Tropical forest / deforestation | train |
| Cerrado | Tropical savanna | train |
| Central Africa (Angola, Zambia, DRC) | Savanna | train |
| West Africa / Sahel | Savanna | train |
| Southeast Asia (Myanmar, Thailand, Laos) | Tropical dry forest / agriculture | train |
| Northern Australia | Savanna | test |
| India / Punjab | Crop residue burning | train |
| Eastern European steppe | Agriculture / steppe | train |
| Mexico / Central America | Tropical dry forest | train |
| Indonesia (Sumatra, Kalimantan) | Peatland / tropical forest | train |

## Test results

The test regions were never used to train the evaluation model. Results on 61,064 clusters:

| Model | Poisson deviance ↓ | MAE ↓ | Detection AUC ↑ | Brier ↓ | 80% interval coverage | Region-day total error (median) ↓ |
|---|---|---|---|---|---|---|
| **FireA-v1** | 0.764 | 0.468 | 0.891 | 0.111 | 94.9% | 20.9% |
| Gradient boosting (Poisson) | 0.788 | 0.478 | 0.889 | 0.113 | 93.8% | 22.5% |
| Power law (μ = a·n^b) | 1.243 | 0.663 | 0.754 | 0.165 | 90.8% | 35.4% |

- FireA is clearly better than the power law. It is also slightly better than gradient boosting on every metric, but the difference is small.
- The 80% intervals contain 94.9% of the real values, so they are a little wider than they need to be.
- Over all test clusters FireA predicted 35,426 MODIS detections; the real number was 34,634.

The model used in the app was retrained with the same settings on all regions except Eastern Mediterranean (Türkiye, Greece); 389,974 clusters in total. We left the Eastern Mediterranean out so that checks in that region, including the Antalya–Muğla box, stay independent.

## How the app uses it

- Predictions assume both satellites look at the same moment (Δt = 0).
- We do not know from which angle MODIS would see the fire, so we predict for 16 different pixel sizes and average them.
- Intervals for daily and monthly totals are computed with Monte Carlo sampling.
- The model uses all VIIRS confidence classes; the quality filter in the interface does not affect it.

## Limitations

- The output is an estimate, not an observation. Fires that MODIS sees but VIIRS misses cannot be recovered this way.
- Cloud information and exact orbit geometry were not used. We inferred whether MODIS looked at an area from nearby detections, so the data may be slightly biased towards areas with many fires.
- Labels come only from Aqua, because Terra does not pass at the same time as VIIRS. The output is therefore an "Aqua-MODIS equivalent".
- The two overpasses can be up to 60 minutes apart and the fire may change in that time. We give this time difference to the model as an input.
- The cluster definition and matching distance are experimental choices we made.
