import boto3
import os
from dotenv import load_dotenv
import time

# Încarcă variabilele din .env
load_dotenv()

# Creează clientul boto3 cu variabilele din .env
transcribe = boto3.client('transcribe')

# Config
job_name = "test-transcription-job-1"
# job_uri = "https://webrtc-transcribe-test.s3.eu-west-1.amazonaws.com/Audio_zone_Coventry_City.mp3?response-content-disposition=inline&X-Amz-Content-Sha256=UNSIGNED-PAYLOAD&X-Amz-Security-Token=IQoJb3JpZ2luX2VjEEgaCWV1LXdlc3QtMSJIMEYCIQCmX837phaEK2%2B%2F54CZ4O%2FpsYJ7YpTgrVnJsJqV0lFryQIhAI%2BXp7mjibE7nn%2B%2FFjhZknyLpERvWeoKIKAQjVW1KhniKqYECEEQAxoMODIxMDk0NDM2NzQ4Igwp0T%2FGNGhKzjol5oEqgwTGfWf%2FpRXPblSAHv%2BKO8br0N8fgJOF%2FiYA3JrtMYq0ClHQPCTRhKeygT%2BpT0Nyw8v50XOJXm%2BLjO8oProSQ%2BDNaukHOwqz3WAYw%2Fa15DC3YbI%2FymuUFm9lehofwiiFGc93QlvpaDQ9FelmWErtpHivra%2F5CKDSG4ystBwIiK5YIh7TclDeZM0moJJN%2Bn2yj55JCZ33%2F8hPuiC27lm2FjqcFa5NAzMb1hfp92em%2BieuwRe%2BIxL%2Fi9KKhi61niPkjMLHcxAIcqAK8EIgR945BfV2SFLSHyuIKjGqqEEjXZBy50XIqZdG0ri3ayYNccAK2LAwHMOq907HVHD0PrJYMMv0qnPOT7MdIWf1zDIH4o%2FvSCULF0yhGTWt0jQnWlaMC%2Bbpxuoa8ftmrcwjr72tEiz%2FHibY8nt8uiCHbuKQumzYaKrJVJw9EepQ1T4QvxQe7OBjX9o4HOZXruA5IoPnqYCP%2B9Wyrgo1hYrFNd8Iy5IpmaSKKSKcfuL7pmafrcRcBfyREF8skzCPsXu2gIYfjNplVoxxb6ONDuEhKHc06DF86m%2BtBoO0WrVIcu1EXa0%2Bsvs2MI6GOzm7ak3i0bjBnYA2eJKutDK8V4VlwxlonNmvmhvUbGL9cCpfb4yvohV%2FBGh1C0S%2Faq%2BOO9%2Bkn%2FH8z5Rx1YinwWz93ajYDNIb8fdIIDyIUTCTwO7CBjq0AmfZVNsplUDIVcZ3UzX%2BUCK%2B0GhOxddutKUANFuwjEHRiJFpWLNCo2MSrLizhp%2F0mR4MUAI66CU63RqGf5cfv5tlyyHaM275GwNLGkMI5Clp9d8l2VYWSWi9IMgrqcXlpkyXrS%2Be0Zg%2F1s5EaOSD3yaB7WBkxFiZZlWNGafQ6HceX5dcImSFtet0dDvSuQSmWw2%2FaYURGf8GbIzRkG9Re5JRkXc4NAXdOneQIUSxwyB4U%2F8msKzk5bV8XhpFGWMhJQ%2FtIeFusXqEnIkNB4VMt9ndbV2NYnzrraSuFSszK1lmWM4C1Td9ET7cpTqQFX24S6h25ZdKKFr1X4i9raRe0y2Ohyz8SvMbuDJ41EUk6JPMTJDIGcMTWZZ1gZEFKtrNkS%2Bzs%2FTCKxHFhNYwJR8hw%2FJJq%2BO7&X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=ASIA36LIKZ6GNFBLSZB4%2F20250625%2Feu-west-1%2Fs3%2Faws4_request&X-Amz-Date=20250625T072719Z&X-Amz-Expires=43200&X-Amz-SignedHeaders=host&X-Amz-Signature=b363f989120a7a372cfc48ed82a484dc3455305a48f59f2bce50a4d3ef2bb02b"  # Transcribe acceptă doar fișiere din S3 sau URL public, nu local!
job_uri = "https://webrtc-transcribe-test.s3.eu-west-1.amazonaws.com/small.mp3"

response = transcribe.start_transcription_job(
    TranscriptionJobName=job_name,
    Media={'MediaFileUri': job_uri},
    MediaFormat='mp3',  # Poate fi wav, mp3, flac etc.
    LanguageCode='en-US'  # Sau 'ro-RO' dacă vrei română și ai activat în AWS
)

print("Transcription job started, waiting for result...")

# Așteptăm până e gata
while True:
    status = transcribe.get_transcription_job(TranscriptionJobName=job_name)
    if status['TranscriptionJob']['TranscriptionJobStatus'] in ['COMPLETED', 'FAILED']:
        break
    print("Still in progress...")
    time.sleep(5)

# Verificăm rezultatul
if status['TranscriptionJob']['TranscriptionJobStatus'] == 'COMPLETED':
    transcript_url = status['TranscriptionJob']['Transcript']['TranscriptFileUri']
    print(f"Transcript URL: {transcript_url}")
else:
    print("Transcription job failed.")
