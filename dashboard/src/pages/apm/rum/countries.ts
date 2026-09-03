/**
 * ISO 3166-1 alpha-2 → the region name used by the bundled world GeoJSON
 * (public/maps/world.json, the ECharts 4 world map) and a display label.
 * Codes not listed fall back to the code itself; they still appear in the
 * table but have no polygon on the map.
 */
export const COUNTRIES: Record<string, { map: string; label: string }> = {
  AF: { map: 'Afghanistan', label: 'Afghanistan' }, AL: { map: 'Albania', label: 'Albania' }, DZ: { map: 'Algeria', label: 'Algeria' },
  AS: { map: 'American Samoa', label: 'American Samoa' }, AD: { map: 'Andorra', label: 'Andorra' }, AO: { map: 'Angola', label: 'Angola' },
  AG: { map: 'Antigua and Barb.', label: 'Antigua and Barbuda' }, AR: { map: 'Argentina', label: 'Argentina' }, AM: { map: 'Armenia', label: 'Armenia' },
  AU: { map: 'Australia', label: 'Australia' }, AT: { map: 'Austria', label: 'Austria' }, AZ: { map: 'Azerbaijan', label: 'Azerbaijan' },
  BS: { map: 'Bahamas', label: 'Bahamas' }, BH: { map: 'Bahrain', label: 'Bahrain' }, BD: { map: 'Bangladesh', label: 'Bangladesh' },
  BB: { map: 'Barbados', label: 'Barbados' }, BY: { map: 'Belarus', label: 'Belarus' }, BE: { map: 'Belgium', label: 'Belgium' },
  BZ: { map: 'Belize', label: 'Belize' }, BJ: { map: 'Benin', label: 'Benin' }, BM: { map: 'Bermuda', label: 'Bermuda' },
  BT: { map: 'Bhutan', label: 'Bhutan' }, BO: { map: 'Bolivia', label: 'Bolivia' }, BA: { map: 'Bosnia and Herz.', label: 'Bosnia and Herzegovina' },
  BW: { map: 'Botswana', label: 'Botswana' }, BR: { map: 'Brazil', label: 'Brazil' }, BN: { map: 'Brunei', label: 'Brunei' },
  BG: { map: 'Bulgaria', label: 'Bulgaria' }, BF: { map: 'Burkina Faso', label: 'Burkina Faso' }, BI: { map: 'Burundi', label: 'Burundi' },
  KH: { map: 'Cambodia', label: 'Cambodia' }, CM: { map: 'Cameroon', label: 'Cameroon' }, CA: { map: 'Canada', label: 'Canada' },
  CV: { map: 'Cape Verde', label: 'Cape Verde' }, KY: { map: 'Cayman Is.', label: 'Cayman Islands' }, CF: { map: 'Central African Rep.', label: 'Central African Republic' },
  TD: { map: 'Chad', label: 'Chad' }, CL: { map: 'Chile', label: 'Chile' }, CN: { map: 'China', label: 'China' },
  CO: { map: 'Colombia', label: 'Colombia' }, KM: { map: 'Comoros', label: 'Comoros' }, CG: { map: 'Congo', label: 'Congo' },
  CD: { map: 'Dem. Rep. Congo', label: 'DR Congo' }, CR: { map: 'Costa Rica', label: 'Costa Rica' }, HR: { map: 'Croatia', label: 'Croatia' },
  CU: { map: 'Cuba', label: 'Cuba' }, CW: { map: 'Curaçao', label: 'Curaçao' }, CY: { map: 'Cyprus', label: 'Cyprus' },
  CZ: { map: 'Czech Rep.', label: 'Czechia' }, CI: { map: "Côte d'Ivoire", label: "Côte d'Ivoire" }, KP: { map: 'Dem. Rep. Korea', label: 'North Korea' },
  DK: { map: 'Denmark', label: 'Denmark' }, DJ: { map: 'Djibouti', label: 'Djibouti' }, DM: { map: 'Dominica', label: 'Dominica' },
  DO: { map: 'Dominican Rep.', label: 'Dominican Republic' }, EC: { map: 'Ecuador', label: 'Ecuador' }, EG: { map: 'Egypt', label: 'Egypt' },
  SV: { map: 'El Salvador', label: 'El Salvador' }, GQ: { map: 'Eq. Guinea', label: 'Equatorial Guinea' }, ER: { map: 'Eritrea', label: 'Eritrea' },
  EE: { map: 'Estonia', label: 'Estonia' }, ET: { map: 'Ethiopia', label: 'Ethiopia' }, FO: { map: 'Faeroe Is.', label: 'Faroe Islands' },
  FK: { map: 'Falkland Is.', label: 'Falkland Islands' }, FJ: { map: 'Fiji', label: 'Fiji' }, FI: { map: 'Finland', label: 'Finland' },
  PF: { map: 'Fr. Polynesia', label: 'French Polynesia' }, FR: { map: 'France', label: 'France' }, GA: { map: 'Gabon', label: 'Gabon' },
  GM: { map: 'Gambia', label: 'Gambia' }, GE: { map: 'Georgia', label: 'Georgia' }, DE: { map: 'Germany', label: 'Germany' },
  GH: { map: 'Ghana', label: 'Ghana' }, GR: { map: 'Greece', label: 'Greece' }, GL: { map: 'Greenland', label: 'Greenland' },
  GD: { map: 'Grenada', label: 'Grenada' }, GU: { map: 'Guam', label: 'Guam' }, GT: { map: 'Guatemala', label: 'Guatemala' },
  GN: { map: 'Guinea', label: 'Guinea' }, GW: { map: 'Guinea-Bissau', label: 'Guinea-Bissau' }, GY: { map: 'Guyana', label: 'Guyana' },
  HT: { map: 'Haiti', label: 'Haiti' }, HN: { map: 'Honduras', label: 'Honduras' }, HU: { map: 'Hungary', label: 'Hungary' },
  IS: { map: 'Iceland', label: 'Iceland' }, IN: { map: 'India', label: 'India' }, ID: { map: 'Indonesia', label: 'Indonesia' },
  IR: { map: 'Iran', label: 'Iran' }, IQ: { map: 'Iraq', label: 'Iraq' }, IE: { map: 'Ireland', label: 'Ireland' },
  IM: { map: 'Isle of Man', label: 'Isle of Man' }, IL: { map: 'Israel', label: 'Israel' }, IT: { map: 'Italy', label: 'Italy' },
  JM: { map: 'Jamaica', label: 'Jamaica' }, JP: { map: 'Japan', label: 'Japan' }, JE: { map: 'Jersey', label: 'Jersey' },
  JO: { map: 'Jordan', label: 'Jordan' }, KZ: { map: 'Kazakhstan', label: 'Kazakhstan' }, KE: { map: 'Kenya', label: 'Kenya' },
  KI: { map: 'Kiribati', label: 'Kiribati' }, KR: { map: 'Korea', label: 'South Korea' }, KW: { map: 'Kuwait', label: 'Kuwait' },
  KG: { map: 'Kyrgyzstan', label: 'Kyrgyzstan' }, LA: { map: 'Lao PDR', label: 'Laos' }, LV: { map: 'Latvia', label: 'Latvia' },
  LB: { map: 'Lebanon', label: 'Lebanon' }, LS: { map: 'Lesotho', label: 'Lesotho' }, LR: { map: 'Liberia', label: 'Liberia' },
  LY: { map: 'Libya', label: 'Libya' }, LI: { map: 'Liechtenstein', label: 'Liechtenstein' }, LT: { map: 'Lithuania', label: 'Lithuania' },
  LU: { map: 'Luxembourg', label: 'Luxembourg' }, MK: { map: 'Macedonia', label: 'North Macedonia' }, MG: { map: 'Madagascar', label: 'Madagascar' },
  MW: { map: 'Malawi', label: 'Malawi' }, MY: { map: 'Malaysia', label: 'Malaysia' }, ML: { map: 'Mali', label: 'Mali' },
  MT: { map: 'Malta', label: 'Malta' }, MR: { map: 'Mauritania', label: 'Mauritania' }, MU: { map: 'Mauritius', label: 'Mauritius' },
  MX: { map: 'Mexico', label: 'Mexico' }, FM: { map: 'Micronesia', label: 'Micronesia' }, MD: { map: 'Moldova', label: 'Moldova' },
  MN: { map: 'Mongolia', label: 'Mongolia' }, ME: { map: 'Montenegro', label: 'Montenegro' }, MS: { map: 'Montserrat', label: 'Montserrat' },
  MA: { map: 'Morocco', label: 'Morocco' }, MZ: { map: 'Mozambique', label: 'Mozambique' }, MM: { map: 'Myanmar', label: 'Myanmar' },
  MP: { map: 'N. Mariana Is.', label: 'Northern Mariana Islands' }, NA: { map: 'Namibia', label: 'Namibia' }, NP: { map: 'Nepal', label: 'Nepal' },
  NL: { map: 'Netherlands', label: 'Netherlands' }, NC: { map: 'New Caledonia', label: 'New Caledonia' }, NZ: { map: 'New Zealand', label: 'New Zealand' },
  NI: { map: 'Nicaragua', label: 'Nicaragua' }, NE: { map: 'Niger', label: 'Niger' }, NG: { map: 'Nigeria', label: 'Nigeria' },
  NU: { map: 'Niue', label: 'Niue' }, NO: { map: 'Norway', label: 'Norway' }, OM: { map: 'Oman', label: 'Oman' },
  PK: { map: 'Pakistan', label: 'Pakistan' }, PW: { map: 'Palau', label: 'Palau' }, PS: { map: 'Palestine', label: 'Palestine' },
  PA: { map: 'Panama', label: 'Panama' }, PG: { map: 'Papua New Guinea', label: 'Papua New Guinea' }, PY: { map: 'Paraguay', label: 'Paraguay' },
  PE: { map: 'Peru', label: 'Peru' }, PH: { map: 'Philippines', label: 'Philippines' }, PL: { map: 'Poland', label: 'Poland' },
  PT: { map: 'Portugal', label: 'Portugal' }, PR: { map: 'Puerto Rico', label: 'Puerto Rico' }, QA: { map: 'Qatar', label: 'Qatar' },
  RO: { map: 'Romania', label: 'Romania' }, RU: { map: 'Russia', label: 'Russia' }, RW: { map: 'Rwanda', label: 'Rwanda' },
  SS: { map: 'S. Sudan', label: 'South Sudan' }, SH: { map: 'Saint Helena', label: 'Saint Helena' }, LC: { map: 'Saint Lucia', label: 'Saint Lucia' },
  WS: { map: 'Samoa', label: 'Samoa' }, SA: { map: 'Saudi Arabia', label: 'Saudi Arabia' }, SN: { map: 'Senegal', label: 'Senegal' },
  RS: { map: 'Serbia', label: 'Serbia' }, SC: { map: 'Seychelles', label: 'Seychelles' }, SL: { map: 'Sierra Leone', label: 'Sierra Leone' },
  SG: { map: 'Singapore', label: 'Singapore' }, SK: { map: 'Slovakia', label: 'Slovakia' }, SI: { map: 'Slovenia', label: 'Slovenia' },
  SB: { map: 'Solomon Is.', label: 'Solomon Islands' }, SO: { map: 'Somalia', label: 'Somalia' }, ZA: { map: 'South Africa', label: 'South Africa' },
  ES: { map: 'Spain', label: 'Spain' }, LK: { map: 'Sri Lanka', label: 'Sri Lanka' }, PM: { map: 'St. Pierre and Miquelon', label: 'Saint Pierre and Miquelon' },
  VC: { map: 'St. Vin. and Gren.', label: 'Saint Vincent and the Grenadines' }, SD: { map: 'Sudan', label: 'Sudan' }, SR: { map: 'Suriname', label: 'Suriname' },
  SZ: { map: 'Swaziland', label: 'Eswatini' }, SE: { map: 'Sweden', label: 'Sweden' }, CH: { map: 'Switzerland', label: 'Switzerland' },
  SY: { map: 'Syria', label: 'Syria' }, ST: { map: 'São Tomé and Principe', label: 'São Tomé and Príncipe' }, TJ: { map: 'Tajikistan', label: 'Tajikistan' },
  TZ: { map: 'Tanzania', label: 'Tanzania' }, TH: { map: 'Thailand', label: 'Thailand' }, TL: { map: 'Timor-Leste', label: 'Timor-Leste' },
  TG: { map: 'Togo', label: 'Togo' }, TO: { map: 'Tonga', label: 'Tonga' }, TT: { map: 'Trinidad and Tobago', label: 'Trinidad and Tobago' },
  TN: { map: 'Tunisia', label: 'Tunisia' }, TR: { map: 'Turkey', label: 'Türkiye' }, TM: { map: 'Turkmenistan', label: 'Turkmenistan' },
  TC: { map: 'Turks and Caicos Is.', label: 'Turks and Caicos Islands' }, VI: { map: 'U.S. Virgin Is.', label: 'U.S. Virgin Islands' }, UG: { map: 'Uganda', label: 'Uganda' },
  UA: { map: 'Ukraine', label: 'Ukraine' }, AE: { map: 'United Arab Emirates', label: 'United Arab Emirates' }, GB: { map: 'United Kingdom', label: 'United Kingdom' },
  US: { map: 'United States', label: 'United States' }, UY: { map: 'Uruguay', label: 'Uruguay' }, UZ: { map: 'Uzbekistan', label: 'Uzbekistan' },
  VU: { map: 'Vanuatu', label: 'Vanuatu' }, VE: { map: 'Venezuela', label: 'Venezuela' }, VN: { map: 'Vietnam', label: 'Vietnam' },
  EH: { map: 'W. Sahara', label: 'Western Sahara' }, YE: { map: 'Yemen', label: 'Yemen' }, ZM: { map: 'Zambia', label: 'Zambia' },
  ZW: { map: 'Zimbabwe', label: 'Zimbabwe' }, AX: { map: 'Aland', label: 'Åland Islands' }, HK: { map: 'China', label: 'Hong Kong' },
  MO: { map: 'China', label: 'Macao' }, TW: { map: 'China', label: 'Taiwan' }, XK: { map: 'Serbia', label: 'Kosovo' },
}

export function countryLabel(code: string): string {
  return COUNTRIES[code]?.label ?? (code || 'Unknown')
}

export function countryMapName(code: string): string | undefined {
  return COUNTRIES[code]?.map
}

/** Regional-indicator flag for a two-letter code (renders on most platforms). */
export function countryFlag(code: string): string {
  if (!/^[A-Z]{2}$/.test(code)) return ''
  return String.fromCodePoint(...[...code].map((char) => 0x1f1e6 + char.charCodeAt(0) - 65))
}
