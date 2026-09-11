#!/usr/bin/env python3
"""Generate the study `notes.md` files that the spec asks for but that were never built.

Two families of notes:

* **GK/GS** — one `notes.md` per topic directory under `database/gk/` that actually
  holds questions.  Each file carries the real distribution (how many questions,
  which exams, which years, which sub-concepts) plus, for the heavy-weight topics,
  a hand-written *Quick revision* block.
* **Grammar** — one `notes.md` per rule leaf under `database/english/grammar/`
  with the rule name, how many questions it owns and how they spread over time.

The generator is **idempotent**: re-running it rewrites the same content.
Run it from the repository root:  `python3 tools/gen_notes.py`
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GK_DIR = ROOT / "database" / "gk"
GRAMMAR_DIR = ROOT / "database" / "english" / "grammar"

# ---------------------------------------------------------------------------
# Hand-written revision blocks.  Keyed by a substring of the topic path, so one
# block can cover a topic and its sub-leaves.  These are the topics that carry
# the overwhelming majority of the questions (see MEMORY.md §11.2).
# ---------------------------------------------------------------------------
AUTHORED_GK = {
    "indian-polity": """\
### Quick revision

- The Constitution was adopted on **26 Nov 1949** and came into force on
  **26 Jan 1950**. It has **25 parts, 12 schedules and 448 articles** (original:
  395 articles, 22 parts, 8 schedules).
- **Parts to remember:** III (Fundamental Rights, Art. 12–35), IV (DPSP, Art. 36–51),
  IV-A (Fundamental Duties, Art. 51-A), V (Union), VI (States), IX (Panchayats),
  IX-A (Municipalities), XIV-A (Tribunals, Art. 323-A/B).
- **Fundamental Rights** — six: Right to Equality (14–18), Freedom (19–22),
  against Exploitation (23–24), Freedom of Religion (25–28), Cultural and
  Educational Rights (29–30), Constitutional Remedies (**Art. 32** — the "heart and
  soul" per Ambedkar).
- **Writs (Art. 32 / 226):** Habeas Corpus, Mandamus, Prohibition, Certiorari,
  Quo Warranto. Supreme Court → Art. 32; High Court → Art. 226.
- **DPSP (Art. 36–51)** borrowed from Ireland; three strands — socialist,
  Gandhian, liberal-intellectual. Art. 37: not enforceable in court.
- **Amendments that matter:** 1st (1951, land reform/9th Sch), 7th, 10th
  (anti-defection), 42nd (1976, "mini-Constitution", added socialist/secular),
  44th (1978, removed Right to Property from FR → Art. 300-A), 52nd (1985,
  anti-defection), 61st (voting age 21→18), 73rd/74th (Panchayati Raj, 1992),
  86th (Right to Education, 2002), 101st/102nd (GST), 103rd (EWS quota).
- **Basic structure doctrine** — *Kesavananda Bharati* (1973), 13-judge bench.
- Legislature: **Lok Sabha** (max 543 elected + 2 nominated; term 5 years) and
  **Rajya Sabha** (max 250; 12 nominated; permanent house, 1/3 retire every 2 years).
- Money Bill → Art. 110; only Lok Sabha origin; President's prior recommendation.
- Impeachment of the President → **Art. 61**; of a judge → Art. 124(4) / 217.
- Emergency: **Art. 352** (National), **356** (President's rule / state),
  **360** (financial).""",
    "art-and-culture": """\
### Quick revision

- **Classical dances (8):** Bharatanatyam (Tamil Nadu), Kathak (UP), Kathakali
  (Kerala), Kuchipudi (Andhra), Odissi (Odisha), Sattriya (Assam), Manipuri
  (Manipur), Mohiniyattam (Kerala).
- **Classical music:** Hindustani (North) vs Carnatic (South).
  *Gharanas:* Gwalior, Agra, Kirana, Jaipur, Patiala (Hindustani).
- **Instruments:** Sitar/Taboo/Sarod (North), Veena/Mridangam (South) —
  Shehnai → Bismillah Khan; Sitar → Ravi Shankar; Sarod → Amjad Ali Khan;
  Flute → Pannalal Ghosh; Santoor → Shivkumar Sharma.
- **Festivals:** Onam (Kerala), Pongal (TN), Bihu (Assam), Baisakhi (Punjab),
  Durga Puja (WB), Ganesh Chaturthi (MH), Navratri (Gujarat), Losar (Ladakh),
  Hornbill (Nagaland).
- **UNESCO heritage (India) — frequently asked:** Taj Mahal, Qutb Minar, Red
  Fort, Ajanta & Ellora, Sun Temple (Konark), Mahabalipuram, Kaziranga, Manas,
  Keoladeo, Sundarbans, Nanda Devi & Valley of Flowers, Western Ghats,
  Great Himalayan NP, Khajuraho, Hampi, Fatehpur Sikri, Darjeeling Railway,
  Chhatrapati Shivaji Terminus, Rani ki Vav, Ahmedabad (historic city).
- **Architecture:** Nagara (North), Dravida (South), Vesara (mixed).
- **Paintings:** Madhubani (Bihar), Warli (Maharashtra), Pattachitra (Odisha),
  Tanjore (TN), Kalamkari (AP), Miniature (Mughal/Rajasthani/Pahari).
- **Handicrafts:** Pashmina (J&K), Chanderi/Maheshwari (MP), Banarasi silk (UP),
  Kanjeevaram (TN), Pochampally (Telangana).""",
    "indian-geography": """\
### Quick revision

- India spans **8°4'N–37°6'N** and **68°7'E–97°25'E**; the **Tropic of Cancer
  (23°30'N)** crosses **8 states** (Gujarat, Rajasthan, MP, Chhattisgarh,
  Jharkhand, West Bengal, Tripura, Mizoram).
- **Physiographic divisions:** Himalayas, Northern Plains, Peninsular Plateau,
  Indian Desert, Coastal Plains, Islands.
- **Himalayan ranges (N→S):** Trans-Himalaya (Karakoram/Ladakh), Greater
  Himadri, Lesser Himachal, Shiwaliks. Passes: Khardung La, Zoji La, Nathu La,
  Bomdi La, Lipulekh.
- **Rivers — Himalayan (perennial):** Indus, Ganga, Brahmaputra.
  **Peninsular (seasonal):** Godavari, Krishna, Kaveri, Mahanadi, Narmada,
  Tapi. Narmada & Tapi flow **west** in rift valleys.
- **Longest:** Ganga (2,525 km) → Godavari (1,465 km, longest peninsular) →
  Krishna → Yamuna (longest tributary) → Brahmaputra.
- **Climate:** tropical monsoon; **South-West monsoon** (June–Sept, ~75% rain),
  **North-East monsoon** (Oct–Dec, Tamil Nadu coast); retreating monsoon.
- **Soils:** Alluvial (most fertile, Indo-Gangetic), Black/Regur (cotton,
  Deccan), Red, Laterite, Desert, Mountain, Saline.
- **Crops:** Rice (West Bengal > UP > Punjab), Wheat (UP > MP > Punjab),
  Sugarcane (UP > Maharashtra), Cotton (Gujarat > Maharashtra), Tea (Assam),
  Coffee (Karnataka), Jute (West Bengal), Rubber (Kerala).
- **Minerals:** Coal (Jharkhand, Odisha, Chhattisgarh), Iron ore (Odisha,
  Chhattisgarh, Karnataka), Bauxite (Odisha), Mica (Jharkhand),
  Petroleum (Assam, Gujarat, Mumbai High).
- **National parks / tiger reserves:** Jim Corbett (first, 1936), Kaziranga
  (one-horned rhino), Gir (Asiatic lion), Sundarbans (Royal Bengal tiger,
  mangrove), Ranthambore, Periyar, Bandhavgarh.""",
    "indian-economy": """\
### Quick revision

- **Planning:** NITI Aayog replaced the Planning Commission (1 Jan 2015).
  Five-Year Plans ran 1951–2017; the **12th** was the last.
- **Sectors:** Primary (agriculture), Secondary (industry), Tertiary (services).
  Services contribute the largest share of GVA (~55%).
- **Key indices:** WPI and CPI (base year 2011-12 for CPI; RBI tracks CPI for
  inflation targeting, target **4% ±2%**).
- **GDP vs GNP:** GDP = domestic production; GNP = GDP + net factor income
  from abroad. **Base year of the current GDP series: 2011-12.**
- **RBI:** established **1 April 1935** (Hilton Young Commission), nationalised
  **1949**; HQ Mumbai; issues currency (except coins → GoI); banker's rate =
  **repo rate**; instruments: CRR, SLR, repo, reverse repo, MSF, LAF.
- **Monetary policy** = RBI (MPC, 6 members); **Fiscal policy** = Government.
- **Taxes:** Income Tax Act 1961; GST from **1 July 2017** (101st Amendment) —
  slabs 0/5/12/18/28; GST Council headed by the Finance Minister.
- **Budget:** Union Budget presented on **1 February** (since 2017, merged
  Railway Budget — a practice started in 1924 on the Acworth Committee's advice).
- **Poverty/employment:** MGNREGA (2005, 100 days), NRLM, Skill India,
  PM-KISAN (₹6,000/yr), PMFBY (crop insurance), e-NAM (agri market).
- **Banking:** Nationalisation 1969 (14 banks) and 1980 (6 banks); SBI is the
  largest; **NABARD** (1982, rural), **SIDBI**, **EXIM Bank**, **NHB**.
- **Stock market:** BSE (Sensex, 30 stocks; established 1875) and NSE
  (Nifty 50; 1992). SEBI established 1988, statutory powers 1992.""",
    "biology": """\
### Quick revision

- **Cell:** Prokaryote (no true nucleus) vs Eukaryote. Organelles — mitochondria
  (powerhouse, ATP), ribosome (protein synthesis), Golgi (packaging), ER (rough →
  protein, smooth → lipid), lysosome (suicide bag), chloroplast (photosynthesis),
  nucleolus (ribosome synthesis).
- **Plant tissues:** Meristematic (apical, lateral, intercalary) and permanent
  (parenchyma, collenchyma, sclerenchyma, xylem, phloem).
- **Photosynthesis:** 6CO₂ + 6H₂O →(light, chlorophyll) C₆H₁₂O₆ + 6O₂.
  Occurs in chloroplast; stomata guard cells regulate gas exchange.
- **Respiration:** Aerobic (→ CO₂ + H₂O + 38 ATP) vs Anaerobic (→ lactic acid in
  muscles; ethanol + CO₂ in yeast).
- **Human systems:** Digestive (amylase→starch, pepsin→protein, lipase→fat;
  small intestine absorbs), Circulatory (heart 4 chambers; RBC carries O₂ via
  haemoglobin; WBC fights infection; platelets clot), Respiratory, Excretory
  (kidney → nephron → urine), Nervous (neuron; brain: cerebrum, cerebellum,
  medulla), Skeletal (206 bones; smallest = stapes in ear; largest = femur).
- **Vitamins & deficiency:** A (night blindness), B1 (beri-beri), B3 (pellagra),
  C (scurvy), D (rickets), E (fertility), K (clotting).
- **Minerals:** Iron (anaemia), Iodine (goitre), Calcium (bones/teeth).
- **Diseases:** Malaria (*Plasmodium*, female Anopheles), Dengue (Aedes aegypti),
  TB (*Mycobacterium*, BCG), Cholera (*Vibrio*), Typhoid, AIDS (HIV),
  Polio, Hepatitis B. Vector-borne vs water-borne — a favourite exam contrast.
- **Genetics:** Mendel = father of genetics; DNA double helix (Watson & Crick,
  1953); genes on chromosomes; dominant/recessive; sex chromosomes XX/XY.
- **Classification (5 kingdoms, Whittaker):** Monera, Protista, Fungi, Plantae,
  Animalia. Binomial nomenclature → Linnaeus.""",
    "modern-indian-history": """\
### Quick revision

- **1857 Revolt** — began at **Meerut (10 May 1857)**; Mangal Pandey;
  leaders: Bahadur Shah Zafar (Delhi), Rani Lakshmibai (Jhansi), Nana Saheb
  (Kanpur), Tantia Tope, Kunwar Singh (Bihar), Begum Hazrat Mahal (Lucknow).
- **Congress founded 1885** (A.O. Hume; first session Bombay, W.C. Banerjee).
  Moderates (1885–1905) → Extremists/Lal-Bal-Pal (1905–1919) → Gandhian era.
- **Partition of Bengal 1905** (Curzon) → Swadeshi Movement; annulled 1911.
  **Muslim League founded 1906** (Dhaka; Aga Khan III).
- **Morley-Minto Reforms 1909** → separate electorates. **Montagu-Chelmsford
  1919** → dyarchy. **Government of India Act 1935** → provincial autonomy,
  federal court, RBI. **Indian Independence Act 1947** → partition, 15 Aug 1947.
- **Gandhian movements:** Champaran (1917, first satyagraha in India),
  Kheda (1918), Ahmedabad mill strike (1918), Rowlatt/Non-Cooperation
  (1920–22, withdrawn after Chauri Chaura), Civil Disobedience (1930 —
  **Dandi March, 12 March 1930**, Salt Law), Quit India (**8 Aug 1942**,
  "Do or Die"), Individual Satyagraha (1940).
- **Jallianwala Bagh — 13 April 1919** (Amritsar, Baisakhi; General Dyer).
- **Revolutionary organisations:** Ghadar Party (1913, San Francisco),
  Hindustan Socialist Republican Association (1928; Bhagat Singh, Chandrashekar
  Azad, Rajguru — **Central Assembly bombing 8 April 1929**;
  Bhagat Singh hanged 23 March 1931).
- **Subhas Chandra Bose:** Congress president (1938 Haripura, 1939 Tripuri
  resigned), founded **Forward Bloc (1939)**, **INA/Azad Hind Fauj (1943)**,
  "Give me blood and I shall give you freedom"; "Jai Hind" slogan.
- **Round Table Conferences (1930–32)** — Gandhi attended the 2nd (1931)
  after the **Gandhi-Irwin Pact**; **Poona Pact 1932** (Ambedkar & Gandhi).
- **Cabinet Mission 1946**, **Cripps Mission 1942**, **Wavell Plan/Simla
  Conference 1945**, **Mountbatten Plan (3 June 1947)** → independence.""",
    "chemistry": """\
### Quick revision

- **Matter:** solid/liquid/gas; **Melting/boiling points**, latent heat.
  **Sublimation:** camphor, naphthalene, iodine, dry ice (solid CO₂).
- **Atomic structure:** Proton (+, 1 amu), Neutron (0, 1 amu), Electron
  (−, 1/1837 amu). Atomic number = protons; Mass number = p + n.
  Isotopes (same Z, different A — e.g. ¹²C/¹⁴C), Isobars (same A, different Z).
- **Periodic table:** 118 elements; Mendeleev arranged by atomic mass,
  Moseley/Modern law by **atomic number**. Groups 18 = noble gases,
  17 = halogens, 1 = alkali metals, 2 = alkaline earth metals.
- **Chemical bonding:** Ionic (e⁻ transfer, e.g. NaCl), Covalent (sharing,
  e.g. H₂O, CH₄), Metallic; Hydrogen bonding (H₂O's high boiling point).
- **Acids/bases/salts:** pH < 7 acidic, > 7 basic, 7 neutral. Indicators:
  litmus, methyl orange, phenolphthalein. Strong acids: HCl, H₂SO₄, HNO₃.
  Bases: NaOH (caustic soda), Ca(OH)₂ (slaked lime), NaHCO₃ (baking soda).
  **Common names:** NaCl → common salt; CaO → quicklime; Ca(OH)₂ → slaked lime;
  CaCO₃ → limestone/chalk/marble; CaSO₄·2H₂O → gypsum (Plaster of Paris =
  CaSO₄·½H₂O); Na₂CO₃ → washing soda; CuSO₄·5H₂O → blue vitriol;
  FeSO₄·7H₂O → green vitriol; MgSO₄·7H₂O → Epsom salt.
- **Oxidation/reduction:** rusting of iron (Fe + O₂ + H₂O → hydrated ferric
  oxide); prevented by galvanisation (Zn coating), painting, alloying.
- **Carbon:** allotropes diamond, graphite, fullerene (C₆₀). Organic:
  homologous series; alkane CₙH₂ₙ₊₂, alkene CₙH₂ₙ, alkyne CₙH₂ₙ₋₂.
- **Water:** hard water (Ca/Mg salts) vs soft; removal by boiling, washing soda,
  ion-exchange.   **Heavy water = D₂O.**
- **Gases:** O₂ (supports combustion, prepared from KClO₃/H₂O₂), H₂ (lightest,
  burns with pop sound), N₂ (78% of air, inert), CO₂ (dry ice, fire
  extinguisher, turns lime water milky), Cl₂ (bleaching, disinfectant).""",
    "physics": """\
### Quick revision

- **Motion:** speed = distance/time; velocity (vector); acceleration.
  Equations: v = u + at, s = ut + ½at², v² = u² + 2as.
- **Newton's laws:** (1) inertia, (2) F = ma / impulse, (3) action-reaction.
  Momentum p = mv; conservation of momentum.
- **Gravity:** g ≈ 9.8 m/s²; G = 6.67×10⁻¹¹ N m²/kg² (Cavendish).
  Weight = mg; mass constant, weight varies. **Escape velocity = 11.2 km/s.**
  Free fall: g is zero at the centre of the earth.
- **Work/energy/power:** W = F·s (joule); KE = ½mv²; PE = mgh;
  Power = W/t (watt). 1 HP = 746 W; 1 kWh = 3.6×10⁶ J.
- **Light:** reflection (plane mirror — image is virtual, erect, laterally
  inverted, same size), refraction (Snell's law; refractive index n = c/v;
  **speed of light = 3×10⁸ m/s**), dispersion (prism → VIBGYOR; red deviates
  least, violet most), scattering (why the sky is blue, sun red at sunset),
  total internal reflection (mirage, optical fibre), **persistence of vision 1/16 s**..
- **Lenses:** power P = 1/f (dioptre); convex (converging, corrects
  hypermetropia), concave (diverging, corrects myopia).
- **Sound:** longitudinal wave; speed ~343 m/s in air, fastest in solids;
  echo needs ≥0.1 s gap (≈17 m); **ultrasound > 20 kHz**, infrasound < 20 Hz;
  audible 20 Hz–20 kHz; SONAR uses ultrasound; loudness in **decibel (dB)**.
- **Heat:** temperature scales (0 °C = 273 K); conduction/convection/radiation;
  specific heat; latent heat (ice 80 cal/g, steam 540 cal/g).
- **Electricity:** V = IR (Ohm); P = VI = I²R; series (same I) vs parallel
  (same V); **SI unit of current = ampere**, charge = coulomb, resistance = ohm,
  potential difference = volt. Fuse wire: low melting point, high resistance
  (tin-lead alloy). Earthing and MCB for safety.
- **Magnetism/EMI:** Oersted (current → magnetism), Faraday's law
  (induced EMF — the basis of generators), Fleming's left-hand rule (motor),
  right-hand rule (generator). **Transformer works on AC only.**
- **Nuclear:** fission (U-235, atom bomb/reactor), fusion (sun/H-bomb);
  **C = 3×10⁸ m/s**; radioactivity discovered by Becquerel; radium by
  Marie Curie; **moderator (heavy water/graphite)** slows neutrons.""",
    "ancient-indian-history": """\
### Quick revision

- **Indus Valley / Harappan (c. 2600–1900 BCE):** discovered 1921 (Daya Ram
  Sahni) at Harappa; Mohenjo-daro (R.D. Banerjee, 1922). Grid town planning,
  Great Bath, granaries, dockyard (Lothal), seals, **no temples**; script
  undeciphered, written right-to-left.
- **Vedic period:** Rigveda (oldest, c. 1500 BCE); Early Vedic (Punjab,
  *sabha/samiti*, no caste rigidity) → Later Vedic (Ganga plain, varna system,
  four ashramas, Upanishads, Brahmanas, Aranyakas).
- **Mahajanapadas (16):** Magadha, Kosala, Vatsa, Avanti, Gandhara, Kuru,
  Panchala... **Magadha** rose to dominance; capitals Rajagriha → Pataliputra.
- **Haryanka dynasty:** Bimbisara, Ajatashatru; **Shishunaga**; **Nanda**
  (Mahapadma Nanda, first non-Kshatriya empire-builder).
- **Mauryas (321–185 BCE):** Chandragupta Maurya (founded with Chanakya/
  Kautilya's help, *Arthashastra*) → Bindusara → **Ashoka (268–232 BCE)**.
  **Kalinga war 261 BCE** → Ashoka embraced Buddhism (Dhamma); edicts
  (Brahmi, Kharosthi, Greek/Aramaic); **Sarnath Lion Capital** is the national
  emblem; **Dhamma Chakra** on the national flag.
- **Post-Mauryan:** Shungas (Pushyamitra), Kanvas, Satavahanas (Gautamiputra
  Satakarni), Kushanas (**Kanishka** — 4th Buddhist council, Gandhara &
  Mathura schools of art), Guptas.
- **Gupta era = "Golden Age of India" (c. 320–550 CE):** Chandragupta I,
  **Samudragupta** (Napoleon of India; Allahabad Pillar inscription by Harisena),
  **Chandragupta II (Vikramaditya)** — Navaratna, Kalidasa in his court;
  **Fa-Hien** visited; Nalanda university.
- **South Indian dynasties:** Cholas (Rajaraja I, Rajendra I; Brihadeeswara
  Temple, Thanjavur; naval power), Cheras, Pandyas (Madurai), Pallavas
  (Mamallapuram), Chalukyas, Rashtrakutas (Ellora Kailasa temple), Vijayanagara
  (Krishnadevaraya; Hampi).
- **Religions:** **Jainism** — 24 Tirthankaras, Rishabhanatha (1st),
  **Mahavira (24th, 540 BCE)**; *ahimsa, anekantavada*; split into
  Digambara/Svetambara. **Buddhism** — **Gautama Buddha (563–483 BCE)**,
  born Lumbini, enlightenment Bodh Gaya, first sermon Sarnath, death
  Kushinagar; four noble truths, eightfold path; councils at Rajagriha,
  Vaishali (2nd), Pataliputra (3rd, Ashoka), Kashmir (4th, Kanishka).""",
    "medieval-indian-history": """\
### Quick revision

- **Delhi Sultanate (1206–1526)** — five dynasties:
  **Slave/Mamluk (1206–90)** — Qutb-ud-din Aibak (Qutb Minar; died 1210 playing
  polo; "Lakh Baksh"), **Iltutmish** (true founder, completed Qutb Minar,
  introduced *Iqta*, silver *tanka*), **Razia Sultana** (only woman ruler);
  **Khalji (1290–1320)** — Alauddin (market reforms, price control, defeated
  Mongols); **Tughlaq (1320–1413)** — **Muhammad bin Tughlaq** (token currency,
  capital shift Daulatabad, "wisest fool"), **Firoz Shah Tughlaq**;
  **Sayyid (1414–51)**; **Lodi (1451–1526)** — **Ibrahim Lodi**, killed at the
  **First Battle of Panipat (1526)**.
- **Vijayanagara Empire (1336–1646):** Harihara & Bukka; **Krishnadevaraya**
  (greatest; *Amuktamalyada*; Ashtadiggajas, Tenali Ramakrishna); capital
  Hampi on the Tungabhadra; fell at **Battle of Talikota (1565)** to the
  Deccan Sultanates.
- **Bahmani Kingdom (1347)** — Alauddin Hasan Bahman Shah (Gulbarga → Bidar);
  broke into the five Deccan Sultanates (Bijapur, Golconda, Ahmednagar,
  Bidar, Berar).
- **Mughals (1526–1857):** **Babur** (Panipat 1526, Khanwa 1527 vs Rana Sanga,
  Ghaghra 1529; *Baburnama*; died 1530) → **Humayun** (lost to Sher Shah at
  Chausa 1539 & Kannauj 1540; **Battle of Panipat 1556** won by his general
  Bairam Khan vs Hemu; Humayun's tomb, Delhi, 1570 — first garden tomb) →
  **Akbar (1556–1605)** (**2nd Panipat / 1556**, Haldighati 1576 vs Maharana
  Pratap; *Din-i-Ilahi*; Navaratna; Todar Mal's revenue system *Zabt/Dahsala*;
  built Fatehpur Sikri, Buland Darwaza; **Ibadat Khana**) → **Jahangir**
  (Chain of Justice; married Nur Jahan; Sir Thomas Roe visited) →
  **Shah Jahan** (**Taj Mahal**, Red Fort, Jama Masjid; Peacock Throne) →
  **Aurangzeb (1658–1707)** (last great Mughal; re-imposed *jizya*;
  Shivaji's clash; Deccan wars drained the empire).
- **Marathas:** **Shivaji** (1630–80; founded Swaraj; **Raigad** capital;
  coronation 1674; *Chhatrapati*; Ashtapradhan council; guerrilla
  *ganimi kava*; **Treaty of Purandar 1665**; escaped from Agra 1666).
  **Balaji Vishwanath**, **Bajirao I** (1720–40, "Ranmard"), Peshwas;
  **Third Battle of Panipat 1761** — Marathas defeated by Ahmad Shah Abdali.
- **Bhakti movement:** Ramanuja, Ramananda, **Kabir**, **Guru Nanak**
  (Sikhism, 1469), **Chaitanya** (Bengal), **Mirabai**, **Surdas**,
  **Tulsidas** (*Ramcharitmanas*), Namdev, Eknath, Tukaram.
- **Sufi orders:** Chishti (Khwaja Moinuddin Chishti, Ajmer), Suhrawardi,
  Qadiri, Naqshbandi; saints Nizamuddin Auliya, Sheikh Salim Chishti.""",
    "books-and-authors": """\
### Quick revision (very high-yield — memorise as pairs)

- **Autobiographies/biographies:** *The Story of My Experiments with Truth* /
  *My Experiments with Truth* — **M.K. Gandhi**; *An Autobiography* /
  *The Indian Struggle*, *The Discovery of India*, *Glimpses of World History*
  — **Jawaharlal Nehru**; *The Indian Struggle 1920–42* — **Subhas Chandra
  Bose**; *Waiting for a Visa* — **B.R. Ambedkar**; *India Wins Freedom* —
  **Abul Kalam Azad**; *My Life and Times* — **V.V. Giri**; *Wings of Fire* —
  **A.P.J. Abdul Kalam**; *The Test of My Life*, *Straight from the Heart* —
  **Sachin Tendulkar**.
- **Fiction/literature:** *Gitanjali* (Nobel 1913), *Gora*, *Ghare Baire*,
  *Chokher Bali* — **Rabindranath Tagore**; *Godan*, *Gaban*,
  *Nirmala* — **Premchand**; *Train to Pakistan*, *I Shall Not Hear the
  Nightingale* — **Khushwant Singh**; *The Guide*, *Swami and Friends*,
  *Malgudi Days* — **R.K. Narayan**; *A Suitable Boy*, *A Passage to India*
  (E.M. Forster), *Untouchable* — **Mulk Raj Anand**; *Midnight's Children*,
  *Satanic Verses* — **Salman Rushdie**; *The God of Small Things* —
  **Arundhati Roy**; *The White Tiger* — **Aravind Adiga**;
  *A Bend in the River* — **V.S. Naipaul**; *The Inheritance of Loss* —
  **Kiran Desai**; *Namesake* — **Jhumpa Lahiri**; *Interpreter of Maladies* —
  **Jhumpa Lahiri**.
- **Non-fiction/polity/economics:** *The Argumentative Indian*,
  *Development as Freedom* — **Amartya Sen**; *India After Gandhi* —
  **Ramachandra Guha**; *Discovery of India* — Nehru; *Hind Swaraj* — Gandhi;
  *Annihilation of Caste* — Ambedkar; *Arthashastra* — **Kautilya/Chanakya**;
  *India Divided* — **Rajendra Prasad**; *The Struggle for India's Soul*.
- **Ancient/classic:** *Ramcharitmanas* — Tulsidas; *Abhijnanasakuntalam* —
  Kalidasa; *Meghaduta*, *Raghuvamsha*, *Kumarasambhava* — Kalidasa;
  *Arthashastra*, *Mudrarakshasa* — Chanakya/Vishakhadatta;
  *Rajatarangini* — **Kalhana** (history of Kashmir);
  *Ain-i-Akbari*, *Akbarnama* — **Abul Fazl**; *Tuzk-i-Babari/Baburnama* —
  Babur (in Turki); *Padmavat* — Malik Muhammad Jayasi;
  *Tirukkural* — **Thiruvalluvar**; *Gita Govinda* — **Jayadeva**.
- **Tip:** exam questions often pair a book with an author from a *different*
  era, so anchor each book to its period and language.""",
    "government-schemes": """\
### Quick revision — the schemes SSC asks about most

- **MGNREGA (2005)** — 100 days of guaranteed rural wage employment;
  Ministry of Rural Development; wage payment through bank/post office.
- **PM-KISAN (2019)** — ₹6,000/year in 3 instalments to landholding farmer
  families; Ministry of Agriculture.
- **Pradhan Mantri Awas Yojana (PMAY)** — housing for all:
  *Gramin* (2016, rural) and *Urban* (2015); interest subsidy (CLSS).
- **Ayushman Bharat (2018)** — **PM-JAY** health cover ₹5 lakh/family/year
  (since increased) + Health & Wellness Centres; the world's largest health
  assurance scheme.
- **Jan Dhan–Aadhaar–Mobile (JAM)** — **PMJDY (2014)** financial inclusion,
  zero-balance accounts, RuPay card, ₹2 lakh accident insurance (later raised).
- **PM Mudra Yojana (2015)** — collateral-free loans to micro units:
  **Shishu (≤₹50k), Kishore (₹50k–₹5 lakh), Tarun (₹5–10 lakh)**.
- **Atal Pension Yojana (2015)** — guaranteed pension for the unorganised
  sector; **NPS**-linked; entry age 18–40.
- **Pradhan Mantri Jeevan Jyoti Bima Yojana (PMJJBY)** — life cover ₹2 lakh,
  premium ₹436/yr; **PMSBY** — accidental cover, premium ₹20/yr (₹12/yr revised).
- **Sukanya Samriddhi Yojana (2015)** — girl child savings under the
  **Beti Bachao Beti Padhao** umbrella; high interest, tax-free.
- **Skill India / PMKVY (2015)** — short-term skill training and
  certification; **Skill India Mission**.
- **Swachh Bharat Mission (2014)** — Urban (2014) and Gramin (2019, Phase II);
  open-defecation-free; toilets.
- **Digital India (2015)** — e-governance, broadband highways, **UMANG**,
  DigiLocker, e-Hospital; **BharatNet** for rural connectivity.
- **Make in India (2014)**, **Startup India / Standup India (2016)**,
  **Ujjwala Yojana (2016)** (free LPG connections, now **Ujjwala 2.0**),
  **Saubhagya (2017)** (electricity to households),
  **Jal Jeevan Mission (2019)** (har ghar nal se jal),
  **PM-Kisan Maandhan**, **Kisan Credit Card**, **e-NAM**, **PMFBY** (crop
  insurance, 2016), **Soil Health Card (2015)**, **Paramparagat Krishi Vikas
  Yojana**, **Rashtriya Krishi Vikas Yojana**.
- **Education:** **Sarva Shiksha Abhiyan**, **Mid-Day Meal / PM POSHAN**,
  **RTE Act 2009** (Art. 21-A, 6–14 years), **Beti Bachao Beti Padhao (2015)**,
  **NEP 2020** (5+3+3+4 structure, mother tongue, multidisciplinary).
- **Tip:** SSC loves *launch year + ministry + target group* — memorise those
  triples rather than long descriptions.""",
    "world-geography": """\
### Quick revision

- **Continents (7):** Asia (largest), Africa, North America, South America,
  Antarctica (coldest, no permanent population), Europe, Australia
  (smallest continent; **Oceania** includes NZ & Pacific islands).
- **Oceans (5):** Pacific (largest & deepest — **Mariana Trench ~11,034 m**,
  Challenger Deep), Atlantic (busiest trade), Indian, Arctic (smallest),
  Southern/Antarctic.
- **Important lines:** Equator (0°), Tropic of Cancer (23½°N), Tropic of
  Capricorn (23½°S), Arctic Circle (66½°N), Antarctic Circle (66½°S),
  Prime Meridian (0°, Greenwich).
- **Highest peaks (continents):** Everest (Asia, 8,849 m) — Aconcagua
  (S. America) — Denali/McKinley (N. America) — Kilimanjaro (Africa) —
  Elbrus (Europe) — Vinson Massif (Antarctica) — **Puncak Jaya / Kosciuszko**
  (Australia debate; Kosciuszko on mainland).
- **Longest rivers:** Nile (traditional longest) vs **Amazon** (largest by
  discharge; contested length) — Yangtze — Mississippi-Missouri — Yenisei —
  Congo — Mekong — Danube (most international).
- **Deserts:** Sahara (largest hot; also Antarctica is the largest cold desert),
  Arabian, Gobi, Kalahari, Atacama (driest), Thar, Great Victoria.
- **Straits/canals:** **Panama Canal** (Pacific–Atlantic; Panama),
  **Suez Canal** (Mediterranean–Red Sea; Egypt), Strait of Gibraltar,
  Bosphorus & Dardanelles, Malacca Strait, Strait of Hormuz (oil chokepoint),
  Bab-el-Mandeb, Cape of Good Hope, Bering Strait.
- **Climate/vegetation:** tropical rainforest (Amazon), savanna (Africa),
  Mediterranean (west coasts, 30–40°), steppe, taiga/coniferous, tundra,
  desert. **Coriolis effect** deflects winds (right in N hemisphere).
- **Time zones:** 24 zones of 15° each; **IST = UTC +5:30**;
  **International Date Line** ≈ 180° meridian (zig-zags).
- **Major mountain ranges:** Andes (longest continental), Rockies, Alps,
  Himalaya, Urals (Europe–Asia boundary), Great Dividing Range, Atlas,
  Caucasus (Europe–Asia), Appalachians.""",
}


def authored_for(relpath: str) -> str:
    """Return the hand-written block for a topic path (longest key wins)."""
    best_key, best_block = "", ""
    for key, block in AUTHORED_GK.items():
        if key in relpath and len(key) > len(best_key):
            best_key, best_block = key, block
    return best_block


def read_jsonl(path: Path):
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def collect(directory: Path):
    """Return (records, sub_leaf_counts) for a topic directory."""
    records = []
    sub_counts = Counter()
    for root, _dirs, files in os.walk(directory):
        root_p = Path(root)
        for name in sorted(files):
            if not name.endswith(".jsonl"):
                continue
            rows = list(read_jsonl(root_p / name))
            records.extend(rows)
            rel = root_p.relative_to(directory).as_posix()
            if rel != ".":
                sub_counts[rel] += len(rows)
    return records, sub_counts


def year_span(records):
    years = [r.get("year") for r in records if isinstance(r.get("year"), int)]
    return (min(years), max(years)) if years else (None, None)


def gk_title(relpath: str) -> str:
    leaf = relpath.replace("_unclassified/", "").split("/")[-1] or "General"
    return leaf.replace("-", " ").replace("_", " ").strip().title()


def build_gk_notes(directory: Path, relpath: str) -> str:
    records, sub_counts = collect(directory)
    if not records:
        return ""
    exams = Counter(r.get("exam") or "?" for r in records)
    years = Counter(r.get("year") for r in records if r.get("year"))
    lo, hi = year_span(records)
    concepts = Counter(
        (r.get("concept") or r.get("concept_raw") or "").strip()
        for r in records
        if (r.get("concept") or r.get("concept_raw"))
    )
    lines = [f"# {gk_title(relpath)}", ""]
    lines.append(f"**{len(records)} questions** in this topic.")
    if lo and hi:
        span = f"{lo}" if lo == hi else f"{lo}–{hi}"
        lines.append(f"Years covered: {span}.")
    if exams:
        lines.append(
            "Exams: "
            + ", ".join(f"{name} ({count})" for name, count in exams.most_common())
            + "."
        )
    lines.append("")

    block = authored_for(relpath)
    if block:
        lines.append(block)
        lines.append("")

    if concepts:
        lines.append("### Most-tested concepts")
        for name, count in concepts.most_common(8):
            lines.append(f"- {name} — {count} question(s)")
        lines.append("")

    if sub_counts:
        lines.append("### Sub-topics")
        for name, count in sub_counts.most_common(12):
            pretty = name.replace("-", " ").replace("_", " ").title()
            lines.append(f"- {pretty} — {count}")
        lines.append("")

    lines.append("### Year-wise spread")
    if years:
        for year in sorted(years):
            lines.append(f"- {year}: {years[year]}")
    else:
        lines.append("- (year not recorded)")
    lines.append("")
    return "\n".join(lines)


def build_grammar_notes(directory: Path) -> str:
    records, _sub = collect(directory)
    rule_name = directory.name.replace("-", " ").title()
    exams = Counter(r.get("exam") or "?" for r in records)
    years = Counter(r.get("year") for r in records if r.get("year"))
    lines = [f"# Grammar — {rule_name}", ""]
    lines.append(f"**{len(records)} question(s)** mapped to this rule.")
    if exams:
        lines.append(
            "Exams: "
            + ", ".join(f"{n} ({c})" for n, c in exams.most_common())
            + "."
        )
    if years:
        lo, hi = min(years), max(years)
        lines.append(f"Years: {lo}" if lo == hi else f"Years: {lo}–{hi}.")
    lines.append("")
    lines.append(
        "See `chapter-and-topic/english-grammar-rules.md` for the authoritative "
        "statement of every rule; the questions under this rule are linked from "
        "`index.md` in this folder."
    )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    written = 0

    if GK_DIR.is_dir():
        for root, dirs, files in os.walk(GK_DIR):
            root_p = Path(root)
            has_questions = any(f.endswith(".jsonl") for f in files)
            if not has_questions:
                continue
            relpath = root_p.relative_to(GK_DIR).as_posix()
            body = build_gk_notes(root_p, relpath)
            if body:
                (root_p / "notes.md").write_text(body, encoding="utf-8")
                written += 1

    grammar_written = 0
    if GRAMMAR_DIR.is_dir():
        for child in sorted(GRAMMAR_DIR.iterdir()):
            if not child.is_dir():
                continue
            if not any(child.glob("*.jsonl")):
                continue
            body = build_grammar_notes(child)
            if body:
                (child / "notes.md").write_text(body, encoding="utf-8")
                grammar_written += 1

    print(f"GK topic notes      : {written}")
    print(f"Grammar rule notes  : {grammar_written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
