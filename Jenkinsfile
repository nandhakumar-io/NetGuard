@Library('netguard-lib') _

pipeline {
    agent any 

    environment {
        DEPLOY_DIR = "/opt/NetGuard/"
        IMAGE_TAG  = "${BUILD_NUMBER}"
        DOCKER_BUILDKIT = "1"
    }

    stages {
        stage('Checkout') {
            steps {
                    checkout scm
                }
            }

        stage('Dependencies') {
            parallel {
                stage('Backend Dependencies') 
                {
                steps {
                    backendDependencies()
                }
            }
                stage('Frontend Dependencies') 
                {
                steps {
                    frontendDependencies()
                }
            }
        }
    }

        stage('Lints') {
            parallel {
                stage('Backend Lint') {
                    steps {
                        backendLint()
                }
            }

                stage('Frontend Lint') {
                steps {
                    frontendLint()
                }
            }
        }
    }

        stage('Tests') {
            parallel {
                stage('backend tests') {
                steps {
                    backendTests()
                }
            }
        }
    }

        // --- Section 19: Supply Chain Security ---
        // Previously nothing in this pipeline scanned dependencies, scanned
        // built images, generated an SBOM, or checked for accidentally
        // committed secrets -- a vulnerable pinned dependency or a leaked
        // credential would ship straight to Deploy with no gate. These
        // stages run as generic `sh` steps (not netguard-lib functions) so
        // they don't require changes to the shared library repo to land.
        // `unstable()` rather than a hard failure for the scanners below:
        // a new CVE in a transitive dependency shouldn't silently block
        // every deploy the moment NVD publishes it, but it must be visible
        // and require a human to look at the build before the next one
        // ships, not disappear into log output nobody reads.
        stage('Dependency Vulnerability Scan') {
            parallel {
                stage('Backend (pip-audit)') {
                    steps {
                        dir('backend') {
                            script {
                                sh 'pip install --break-system-packages -q pip-audit || true'
                                def exitCode = sh(script: 'export PATH="$HOME/.local/bin:$PATH" && pip-audit -r requirements.txt -f json -o pip-audit-report.json', returnStatus: true)
                                if (exitCode != 0) {
                                    echo "pip-audit found issues -- see pip-audit-report.json"
                                    currentBuild.result = 'UNSTABLE'
                                }
                            }
                        }
                        archiveArtifacts artifacts: 'backend/pip-audit-report.json', allowEmptyArchive: true
                    }
                }
                stage('Frontend (npm audit)') {
                    steps {
                        dir('frontend') {
                            script {
                                def exitCode = sh(script: 'npm audit --omit=dev --audit-level=high --json > npm-audit-report.json', returnStatus: true)
                                if (exitCode != 0) {
                                    echo "npm audit found high/critical issues -- see npm-audit-report.json"
                                    currentBuild.result = 'UNSTABLE'
                                }
                            }
                        }
                        archiveArtifacts artifacts: 'frontend/npm-audit-report.json', allowEmptyArchive: true
                    }
                }
            }
        }

        stage('Secret Scan') {
            steps {
                // gitleaks over the full checked-out history of this build,
                // not just the working tree, since a secret committed and
                // later removed is still compromised (git history retains
                // it). Section 19: "Never commit .env / private keys /
                // passwords / API tokens / Keycloak admin credentials /
                // OpenBao root-unseal credentials" -- this is the gate that
                // actually checks that, rather than relying on .gitignore
                // and code review alone.
                script {
                    // Use native gitleaks exit code instead of relying on the pipeline-utility-steps readJSON DSL
                    def exitCode = sh(
                        script: 'docker run --rm -v "$PWD:/repo" zricethezav/gitleaks:latest detect --source=/repo --report-path=/repo/gitleaks-report.json',
                        returnStatus: true
                    )
                    if (exitCode != 0) {
                        echo "gitleaks found potential secret(s) or encountered an error -- see gitleaks-report.json"
                        currentBuild.result = 'UNSTABLE'
                    }
                }
                archiveArtifacts artifacts: 'gitleaks-report.json', allowEmptyArchive: true
            }
        }

        stage('Static Analysis (Bandit)') {
            steps {
                // ruff (backendLint) covers style/correctness; bandit
                // specifically covers security-relevant Python patterns
                // (Section 8/19: command injection, unsafe deserialization,
                // hardcoded credentials, weak crypto) that a style linter
                // isn't designed to catch.
                dir('backend') {
                    script {
                        sh 'pip install --break-system-packages -q bandit || true'
                        def exitCode = sh(script: 'export PATH="$HOME/.local/bin:$PATH" && bandit -r app -f json -o bandit-report.json -ll', returnStatus: true)
                        if (exitCode != 0) {
                            echo "bandit found medium+/high-severity issues -- see bandit-report.json"
                            currentBuild.result = 'UNSTABLE'
                        }
                    }
                }
                archiveArtifacts artifacts: 'backend/bandit-report.json', allowEmptyArchive: true
            }
        }
        
        stage('Build  Images') {
            parallel {
                stage('Backend') {
                    steps {
                        dockerBuild(
                            image: "ghcr.io/nandhakumar-io/netguard-backend",
                            tag: IMAGE_TAG,
                            context: "./backend"
                        )
            }
        }

                stage('Frontend') {
                    steps {
                        dockerBuild(
                            image: "ghcr.io/nandhakumar-io/netguard-frontend",
                            tag: IMAGE_TAG,
                            context: "./frontend"
                        )
                }
            }
        }
    }

        stage('Image Vulnerability Scan') {
            // Runs after build, before push -- an unscanned image was
            // previously indistinguishable from a scanned-and-clean one by
            // the time it reached Deploy. CRITICAL findings hard-fail the
            // build (Push/Deploy never run for that image); HIGH findings
            // mark the build UNSTABLE so it's visible without blocking
            // every deploy on every transitive HIGH the moment a scanner
            // vendor updates its severity feed.
            parallel {
                stage('Backend Image') {
                    steps {
                        sh '''
                            docker run --rm \
                                -v /var/run/docker.sock:/var/run/docker.sock \
                                -v "$PWD:/report" \
                                aquasec/trivy:latest image \
                                --format json --output /report/trivy-backend-report.json \
                                --severity HIGH,CRITICAL \
                                ghcr.io/nandhakumar-io/netguard-backend:${IMAGE_TAG}

                            docker run --rm \
                                -v /var/run/docker.sock:/var/run/docker.sock \
                                aquasec/trivy:latest image \
                                --exit-code 1 --severity CRITICAL --ignore-unfixed \
                                ghcr.io/nandhakumar-io/netguard-backend:${IMAGE_TAG}
                        '''
                        archiveArtifacts artifacts: 'trivy-backend-report.json', allowEmptyArchive: true
                    }
                }
                stage('Frontend Image') {
                    steps {
                        sh '''
                            docker run --rm \
                                -v /var/run/docker.sock:/var/run/docker.sock \
                                -v "$PWD:/report" \
                                aquasec/trivy:latest image \
                                --format json --output /report/trivy-frontend-report.json \
                                --severity HIGH,CRITICAL \
                                ghcr.io/nandhakumar-io/netguard-frontend:${IMAGE_TAG}

                            docker run --rm \
                                -v /var/run/docker.sock:/var/run/docker.sock \
                                aquasec/trivy:latest image \
                                --exit-code 1 --severity CRITICAL --ignore-unfixed \
                                ghcr.io/nandhakumar-io/netguard-frontend:${IMAGE_TAG}
                        '''
                        archiveArtifacts artifacts: 'trivy-frontend-report.json', allowEmptyArchive: true
                    }
                }
            }
        }

        stage('SBOM Generation') {
            // Section 19: "SBOM generation." One CycloneDX SBOM per image,
            // archived alongside the build so "what's actually in the
            // image we deployed on date X" is answerable later without
            // re-pulling and re-scanning it.
            parallel {
                stage('Backend SBOM') {
                    steps {
                        sh '''
                            docker run --rm \
                                -v /var/run/docker.sock:/var/run/docker.sock \
                                anchore/syft:latest \
                                ghcr.io/nandhakumar-io/netguard-backend:${IMAGE_TAG} \
                                -o cyclonedx-json > sbom-backend.json
                        '''
                        archiveArtifacts artifacts: 'sbom-backend.json'
                    }
                }
                stage('Frontend SBOM') {
                    steps {
                        sh '''
                            docker run --rm \
                                -v /var/run/docker.sock:/var/run/docker.sock \
                                anchore/syft:latest \
                                ghcr.io/nandhakumar-io/netguard-frontend:${IMAGE_TAG} \
                                -o cyclonedx-json > sbom-frontend.json
                        '''
                        archiveArtifacts artifacts: 'sbom-frontend.json'
                    }
                }
            }
        }

        stage('Push Images') {
                    steps {
                        script {

                            dockerLogin()

                            parallel (
                                backend: {
                                    dockerPush(
                                        image: "ghcr.io/nandhakumar-io/netguard-backend",
                                        tag: IMAGE_TAG
                                    )
                                },

                                frontend: {
                                    dockerPush(
                                        image: "ghcr.io/nandhakumar-io/netguard-frontend",
                                        tag: IMAGE_TAG
                                    )
                                }
                            )
                        }
                    }
                }
        stage('Deploy') {

            steps {

                script {

                    try {

                        deploy(IMAGE_TAG)

                        healthCheck()

                        updateVersion(IMAGE_TAG)
                    }

                    catch (Exception e) {

                        rollback()
                        
                        throw e 
                }
            }
        }
    }
}


    post {
        success {
                echo "Build Successful"
            }
        failure {
            echo "Build failure !..."
        }
        always {
            cleanWs()
            dockerLogout()
        }
    }
}