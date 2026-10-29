# pdf trans

Devin Session `b8453f276e0a`

## User · 2026-10-27T20:51:35.780Z

hagamos research
tengo este problema para resolver que esta como adjunto en el archivo .md
quiero ordenar las siguientes ideas en un plan y/o por prioridad para desarrollar un prototipo. haz un reporte en html

usar pdfs para testear: happy path = simple, dificiles chiquitos edge cases idiomas a traducir tipo ingles aleman arabe chino, por que elegimos estos? porque son diferentes entre si tienen distintas caracteristicas para probar los resultados aleman caracteres latinos umlaut etc, arabe ltr, chino kanji en vez de silabas, evalua si hay algunos mejores para testear, la idea es que puede ser que haya problemas de overflow de texto que se salga de las cajas al traducir, queremos que el pdf quede igual en estructura en lo posible, con la misma tipografia e imagenes posicionadas igual. El ultimo ejemplo tiene que ser un manual de una moto stark

duda: como evaluar/eval? como saber que calidad tiene la traduccion?

pedir a la ia formas de evaluar el pdf, que no hubo overflow, sacar screenshots y comparar? que me dice la ia sobre como evaluar calidad del output?

opcional, modelo adversario para controlar la calidad del texto producido en el segundo idioma

control de calidad, offline u online? en tiempo de desarrollo o runtime y le mostramos al user la quality obtenida

implementar loop con evaluador? iterar varias veces hasta llegar a la quality de threshold, con un break de iteraciones


opcionales:
seleccionar tipo de traduccion: tecnica, formal, etc, identificar desde el texto

detectar palabras de jerga y evaluar como traducirlas. jerga-detection

modelo evaluador, detectar, ver opciones, al mismo estilo o estilos similares

que pasa si un pdf tiene muchos estilos distintos?

fase 1 detectar estilo y jerga
fase 2 traducir
fase 3 presentar ui con las decisiones tomadas y tunning
moto stark = moto fuerte? esto seria una mala traduccion, no queremos que traduzca la palabra stark. En jerga-detection lo que sucederia es que hay una primera pasada de la ia por el pdf que detecta las palabras de jerga y le ofrece al usuario la lista de palabras para que el usuario pueda seleccionar si quiere o no quiere traducirlas. Por defecto todas como que si las traduce.


adversarial si la traduccion es mala y esta en el loop de refinamiento, si no converge, el paso ultimo seria pasar imagen
y convertir via ocr

Attachments: `.sessionboxer/uploads/5014183e/ai-engineer-assessment-2-.md`

## User · 2026-10-27T20:57:30.950Z

fix the colors of the report to be light instead of dark, there is too much contrast its killing my eyes

## User · 2026-10-27T23:27:14.808Z

ok, make a first implementation P0 and P1. to eval, samples, use short pdfs max 5 pages, for the stark manual, get the first 20 pages only, that way we develop with not so big data. push the samples to the repo. I have an openai api key we need to use for the llm/agent part of the runtime, so make it an env variable.
configure the repository to deploy it in coolify.

## User · 2026-10-27T23:57:13.965Z

in /whorkspace/apikey we have the openaiapikey that we can use for the llm/agent part of the application when deployed, and also for developing now

## User · 2026-10-28T00:26:11.218Z

usa para los commits jperelli@gmail.com force push

## User · 2026-10-28T00:29:10.455Z

carga la variable OPENAI_API_KEY  y deploya el repo en coolify, dame la url final

## User · 2026-10-28T10:42:41.864Z

I need to understand how we are doing the evaluation, because for example I see this translation with a lot of overlap in text, and that should be unacceptable
I send you also an example where it seems one typography does not have special characters, give me ideas on how to handle that, because it looks awful. Should we create the special characters that dont exist in that typography somehow? that might be too much.. should we flag that and propose different alternatives at the end?
It seems to me it depends on the purpose of the translation. If this is only to understand the text or this should be a professional looking final pass. Either way, overlapping text is not acceptable.
Characters not matching original text might be flagged as warning and offer at the end to fix them in a second pass with alternative methods that could be: changing typography to another similar one that has all characters available or using the llm to build missing characters into the typography. So we need that final step of warnings/things that didnt work and be able to choose some alternatives and redo with those decitions

Attachments: `.sessionboxer/uploads/bbb68e64/image.png`, `.sessionboxer/uploads/fa98b887/image.png`, `.sessionboxer/uploads/fd1229bb/image.png`

## User · 2026-10-28T11:05:04.974Z

ok, write that in a plan to implement things, 
We need the evaluation changes
Then for the missing characters, I'd need to know how expensive the "Build accented letters from pieces of the same font." is in terms of adding that by default, would that increase the tokens usage too much? add to the final screen how much was spent on doing that
write in the plan too, that the end screen could be a stats screen where we could see how many tokens were spent in evaluation, back and forth, per page, per the full document, how much in building the accented glyphs,
then we need also a screen where we can see a table of the previous runs to see the summary and download a previous one.
we need to see the log of the run, the one that we see in the "progress" screen Id like to see it in a tab in the final screen
then the final screen should have the download button on the top and the quality score, remove the blocks of stats from the top and just add tabs, first tab is "preview", second tab is "details" which contains tabs jargon (current decitions),  "refinement" which is the current "blocks and issues",  stats which is the current cost and trace with added graphics and log which is the progress but seen there in the final part. Then the last top tab should be the "warnings" or something like that "review" maybe, which shows the new decitions that we might select to redo with this fixes/refine

write all this in a plan that i can review to start implementing

## User · 2026-10-28T11:23:27.731Z

Headline score show the lower
similar-font have both, if the bundled set works, use it, if not go to google fonts to get one at runtime
add sqlite for persistence
use the real cv as a sample just change the name and address, pii, leave the photo
hardcode current list prices yes

with that, start implementing the plan

## User · 2026-10-28T12:34:08.613Z

ok, now i want observability into all of this. Lets research alternatives: Can we use grafana and store all tempo spans for this? also what can we use to store errors? like sentry, but something self hosted, can we use grafana? or do we have anything better? and for ui usage/screen recording? do we have anything self hosted?

## User · 2026-10-28T15:01:27.343Z

what alternatives are there as queues for this, so that each pdf conversion goes into a job in a queue? it should be one that we can get the progress from and we could keep it simple with sqlite as backend. Also for event communication between app and queue can we use the same sqlite? is there any alternative for that. I only know celery, but there must be some other to use in python

## User · 2026-10-28T15:24:54.403Z

i think huey would be the best. I'd directly use the events table, and i dont fully understand this, explain more "Las tareas son sincrónicas; nuestro pipeline asíncrono correría con asyncio.run() dentro de la tarea." and some good bad about that

## User · 2026-10-28T15:37:27.291Z

no termino de entender la diferencia que me estas diciendo entre que huey es sync? cual seria el problema si tenemos una queue y cuando termina de procesar algo, huey agarra lo siguiente? vs ahora es async?

## User · 2026-10-28T15:47:33.059Z

si, implementalo

## User · 2026-10-28T16:12:38.815Z

remove this text: "Upload a document, pick a language. Agents analyse it, translate page by page, check every block against the original layout and a quality reviewer, fix what fails and tell you what still needs a decision."

instead of `understand` and `publish`, put it as `fast` and `quality`
- fast:
  - fast and cheap
  - good translation
  - no thorough style check

- quality
  - slow and more expensive
  - good translation
  - checks thoroughly styling and fixes to layout mismatch

also default selected should be the fast one

remove this: "Live · gpt-5.4-mini" indicator, i dont know what it is doing there.

## User · 2026-10-28T16:18:07.279Z

my cv has a typography ubuntu sans, when translating i don't understand why it's getting changed to a serif typography and the looks is totally different.

## User · 2026-10-28T16:35:04.459Z

move the "or try a sample" to inside the "drop a pdf" on the top right have a button, on click open a modal to select one of the samples, when selected fill the "drop a pdf" the same as if a document had been selected

## User · 2026-10-28T16:39:59.346Z

add interactivity to the detail graphs, maybe use a charting library in frontend? the idea is to show the numbers on click/hover

## User · 2026-10-28T16:46:56.238Z

push to GitHub and deploy to Coolify

## User · 2026-10-28T18:25:00.259Z

Lets research possible ways to make the translation faster and cheaper without downgrading quality, what options do we have?

## User · 2026-10-28T18:31:12.931Z

i want to be able to monitor the queues and huey, maybe an admin ui? or can we use grafana for this?

## User · 2026-10-28T20:03:13.698Z

ok, do both

## User · 2026-10-28T22:38:10.277Z

Lets do the improvements in speed and cost with these three
- Glossary only with the terms in each batch, analysis capped (~30 terms), shorter judge output that lists only errors
- Make the prompt cache hit (fixed prompt start, cache key per job)
- Load the Chinese font once instead of once per block

## User · 2026-10-28T23:07:06.191Z

can we expose an MCP server so that an agent can use our service through mcp interface? as this is a long process sometimes, can we use SSE?

## User · 2026-10-28T23:19:42.339Z

yes lets do it

## User · 2026-10-28T23:38:12.337Z

now create a landing page with the functionalities and features

## User · 2026-10-28T23:53:53.882Z

can you create an architecture diagram of the internal working of the application? and add it to the landing page

## User · 2026-10-29T00:02:30.402Z

push and deploy everything
